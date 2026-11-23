"""MVSync library: urls.txt parsing, playlist expansion, ingest (spec §3)."""
import json
import os
import queue
import shutil
import subprocess
import threading
import time
from datetime import datetime
from pathlib import Path
from urllib.parse import parse_qs, urlparse

import mvsync_features as F

YT_HOSTS = ("youtube.com", "www.youtube.com", "m.youtube.com", "music.youtube.com",
            "youtube-nocookie.com", "www.youtube-nocookie.com")


# ------------------------------------------------------------------ urls.txt
def parse_line(line):
    """Return dict(kind=video|playlist, id, url, list, warn) or None."""
    raw = line.split("#", 1)[0].strip()
    if not raw:
        return None
    force_pl = False
    if raw.lower().startswith("playlist:"):
        force_pl = True
        raw = raw.split(":", 1)[1].strip()
    if "://" not in raw:
        raw = "https://" + raw
    u = urlparse(raw)
    host = (u.hostname or "").lower()
    qs = parse_qs(u.query)
    vid = None
    lst = qs.get("list", [None])[0]
    if host in ("youtu.be", "www.youtu.be"):
        vid = u.path.strip("/").split("/")[0] or None
    elif host in YT_HOSTS:
        parts = [p for p in u.path.split("/") if p]
        if parts and parts[0] == "watch":
            vid = qs.get("v", [None])[0]
        elif len(parts) >= 2 and parts[0] in ("shorts", "live", "embed", "v"):
            vid = parts[1]
    else:
        return {"kind": "invalid", "url": raw, "warn": f"not a YouTube URL: {line.strip()}"}
    if vid is not None and len(vid) != 11:
        return {"kind": "invalid", "url": raw, "warn": f"bad video id in: {line.strip()}"}
    warn = None
    if lst and lst.startswith("RD"):
        warn = f"auto-generated mix (list={lst}) is not expanded"
        if vid:
            return {"kind": "video", "id": vid, "url": f"https://www.youtube.com/watch?v={vid}",
                    "warn": warn}
        return {"kind": "invalid", "url": raw, "warn": warn}
    if lst and (vid is None or force_pl):
        return {"kind": "playlist", "id": lst,
                "url": f"https://www.youtube.com/playlist?list={lst}", "warn": None}
    if vid:
        return {"kind": "video", "id": vid, "url": f"https://www.youtube.com/watch?v={vid}",
                "warn": None}
    return {"kind": "invalid", "url": raw, "warn": f"no video id: {line.strip()}"}


def parse_urls(text):
    out, seen = [], set()
    for line in text.splitlines():
        e = parse_line(line)
        if e is None:
            continue
        key = (e["kind"], e.get("id"))
        if e["kind"] != "invalid" and key in seen:
            continue
        seen.add(key)
        out.append(e)
    return out


# ------------------------------------------------------------------ yt-dlp
class YtDlp:
    def __init__(self, exe, args=None, node_dir=None, log=print):
        self.exe = os.path.expanduser(exe)
        self.args = list(args or [])
        self.node_dir = os.path.expanduser(node_dir) if node_dir else None
        self.log = log

    def env(self):
        env = dict(os.environ)
        if self.node_dir:
            env["PATH"] = self.node_dir + os.pathsep + env.get("PATH", "")
        return env

    def run(self, args, timeout=600):
        cmd = [self.exe] + self.args + list(args)
        p = subprocess.run(cmd, capture_output=True, text=True, env=self.env(), timeout=timeout)
        if p.returncode != 0:
            err = [l for l in p.stderr.splitlines() if "ERROR" in l] or p.stderr.splitlines()[-3:]
            raise RuntimeError("\n".join(err) or f"yt-dlp exit {p.returncode}")
        return p.stdout

    def json(self, args, timeout=300):
        return json.loads(self.run(["-J"] + list(args), timeout=timeout))

    def version(self):
        try:
            return subprocess.run([self.exe, "--version"], capture_output=True, text=True,
                                  timeout=30).stdout.strip()
        except Exception as e:  # noqa
            return f"unavailable ({e})"

    def direct_url(self, url, fmt="18", wait_valid=20.0):
        """Direct googlevideo URL. YouTube answers 403 for the first few seconds after
        extraction (yt-dlp itself 'sleeps 5 s as required by the site'), so poll with a small
        range request until it is served before handing it to TouchDesigner."""
        out = self.run(["-f", fmt, "-g", "--no-playlist", url], timeout=120)
        u = out.strip().splitlines()[0]
        import urllib.request
        t_end = time.time() + wait_valid
        while time.time() < t_end:
            try:
                req = urllib.request.Request(u, headers={"Range": "bytes=0-1023"})
                with urllib.request.urlopen(req, timeout=5) as r:
                    if r.status in (200, 206):
                        return u
            except Exception:
                pass
            time.sleep(1.0)
        self.log(f"direct URL still not served after {wait_valid:.0f}s; sending anyway")
        return u

    def search(self, query, n=8):
        d = self.json([f"ytsearch{n}:{query}", "--flat-playlist"])
        return d.get("entries") or []


def now_iso():
    return datetime.now().isoformat(timespec="seconds")


# ------------------------------------------------------------------ library
class Library:
    """Owns ~/MVSync/{urls.txt,library/,library_index.json,playlists.json}."""

    PLAYLIST_REFRESH_S = 30 * 60
    CONFIRM_LIMIT = 200

    def __init__(self, root, ytdlp, ffmpeg="ffmpeg", log=print, on_change=None,
                 on_ready=None, on_confirm=None):
        self.root = Path(os.path.expanduser(root))
        self.libdir = self.root / "library"
        self.urls = self.root / "urls.txt"
        self.index_path = self.root / "library_index.json"
        self.pl_path = self.root / "playlists.json"
        self.yt = ytdlp
        self.ffmpeg = ffmpeg
        self.log = log
        self.on_change = on_change or (lambda p: None)
        self.on_ready = on_ready or (lambda vid, onset: None)
        self.on_confirm = on_confirm or (lambda pid, title, n: None)
        self.libdir.mkdir(parents=True, exist_ok=True)
        if not self.urls.exists():
            self.urls.write_text("# 1 行に 1 つ YouTube の URL（動画 / プレイリスト）を書く。# 以降はコメント\n",
                                 encoding="utf-8")
        self.lock = threading.RLock()
        self.metas = {}
        self.playlists = {}      # pid -> {id,title,url,video_ids,refreshed}
        self.q = queue.PriorityQueue()   # (0 = priority / 1 = normal, time, vid, url)
        self.queued = set()
        self.pending_confirm = {}  # pid -> entries
        self._urls_mtime = None
        self.quality = 360       # max video height to download (360 = YouTube format 18)
        self.upgrade = set()     # ready videos queued for a re-download at self.quality
        self._last_pl_refresh = 0.0
        self._stop = threading.Event()
        self._load()

    # -------------------------------------------------------------- persistence
    def _load(self):
        for d in self.libdir.iterdir() if self.libdir.exists() else []:
            m = d / "meta.json"
            if m.exists():
                try:
                    self.metas[d.name] = json.loads(m.read_text(encoding="utf-8"))
                except Exception as e:  # noqa
                    self.log(f"bad meta {m}: {e}")
        if self.pl_path.exists():
            try:
                self.playlists = json.loads(self.pl_path.read_text(encoding="utf-8"))
            except Exception:
                self.playlists = {}

    def meta(self, vid):
        return self.metas.get(vid)

    def _write_meta(self, vid, **kw):
        with self.lock:
            m = self.metas.setdefault(vid, {"id": vid, "url": f"https://www.youtube.com/watch?v={vid}",
                                            "added_at": now_iso(), "playlists": [],
                                            "source": "list", "status": "queued"})
            m.update(kw)
            d = self.libdir / vid
            d.mkdir(parents=True, exist_ok=True)
            tmp = d / "meta.json.tmp"
            tmp.write_text(json.dumps(m, ensure_ascii=False, indent=1), encoding="utf-8")
            os.replace(tmp, d / "meta.json")
            return m

    def write_index(self):
        with self.lock:
            entries = []
            for vid, m in sorted(self.metas.items(), key=lambda kv: kv[1].get("added_at", "")):
                e = {k: m.get(k) for k in ("id", "title", "channel", "duration", "status",
                                           "source", "reason", "playlists", "added_at")}
                e["file"] = str(self.libdir / vid / "video.mp4")
                entries.append(e)
            pls = [{"id": p["id"], "title": p.get("title", ""), "url": p.get("url"),
                    "video_ids": p.get("video_ids", [])} for p in self.playlists.values()]
            data = {"updated": now_iso(), "root": str(self.root), "entries": entries,
                    "playlists": pls}
            tmp = self.index_path.with_suffix(".json.tmp")
            tmp.write_text(json.dumps(data, ensure_ascii=False, indent=1), encoding="utf-8")
            os.replace(tmp, self.index_path)
            tmp = self.pl_path.with_suffix(".json.tmp")
            tmp.write_text(json.dumps(self.playlists, ensure_ascii=False, indent=1), encoding="utf-8")
            os.replace(tmp, self.pl_path)
        self.on_change(str(self.index_path))

    def ready_ids(self):
        return [v for v, m in self.metas.items() if m.get("status") == "ready"]

    def video_file(self, vid):
        return str(self.libdir / vid / "video.mp4")

    # -------------------------------------------------------------- refs
    def load_refs(self):
        """Yield (vid, onset) for ready entries, rebuilding stale features."""
        for vid in list(self.ready_ids()):
            p = self.libdir / vid / "ref.npz"
            onset = None
            if p.exists():
                try:
                    onset = F.load_ref(p)
                except Exception as e:  # noqa
                    self.log(f"{vid}: bad ref.npz ({e})")
            if onset is None:
                try:
                    self.log(f"{vid}: rebuilding features (version {F.FEAT_VERSION})")
                    onset = self.analyze(vid)
                except Exception as e:  # noqa
                    self._write_meta(vid, status="failed", reason=f"analyze: {e}")
                    continue
            yield vid, onset

    def analyze(self, vid, media=None):
        media = media or self.video_file(vid)
        y = F.decode_audio(media, self.ffmpeg)
        feat = F.features_full(y)
        F.save_ref(self.libdir / vid / "ref.npz", feat)
        return feat

    # -------------------------------------------------------------- queue
    def enqueue(self, vid, url=None, source="list", playlist=None, priority=False, title=None):
        with self.lock:
            m = self.metas.get(vid)
            if playlist is not None:
                m = self._write_meta(vid, **({} if m else {"source": source,
                                                           "title": title or vid}))
                pls = [p for p in m.get("playlists", []) if p.get("id") != playlist["id"]]
                pls.append(playlist)
                m["playlists"] = pls
                self._write_meta(vid, playlists=pls)
            if m and m.get("status") in ("ready", "unavailable", "removed") and not priority:
                return False
            if m and m.get("status") == "failed" and not priority:
                return False
            if m and m.get("status") in ("downloading", "analyzing") and not priority:
                return False      # being ingested right now (e.g. urls.txt saved meanwhile)
            if vid in self.queued:
                if not priority:
                    return False
                self.q.put((0, time.time(), vid, url))   # jump ahead; the older entry is skipped
                return True
            if not m:
                self._write_meta(vid, source=source, title=title or vid, status="queued")
            else:
                self._write_meta(vid, status="queued", reason=None)
            self.queued.add(vid)
        self.q.put((0 if priority else 1, time.time(), vid, url))
        return True

    def retry(self, vid):
        return self.enqueue(vid, priority=True)

    def remove(self, vid):
        """Delete the downloaded MV (video + features) but keep meta.json with status=removed,
        so urls.txt / playlist refreshes do not download it again. Retry restores it."""
        with self.lock:
            if vid not in self.metas:
                return False
            d = self.libdir / vid
            for f in list(d.glob("video.*")) + list(d.glob("dl.*")) + [d / "ref.npz"]:
                try:
                    f.unlink()
                except FileNotFoundError:
                    pass
            self.queued.discard(vid)
            self._write_meta(vid, status="removed", reason="removed from TD", removed_at=now_iso())
        self.write_index()
        self.log(f"removed: {vid} {self.metas[vid].get('title')}")
        return True

    # -------------------------------------------------------------- urls.txt watch
    def check_urls(self, force=False):
        try:
            mt = self.urls.stat().st_mtime
        except FileNotFoundError:
            return
        changed = mt != self._urls_mtime
        if not changed and not force:
            if time.time() - self._last_pl_refresh > 60:
                self.refresh_playlists(stale_only=True)
            return
        self._urls_mtime = mt
        entries = parse_urls(self.urls.read_text(encoding="utf-8", errors="replace"))
        added = 0
        for e in entries:
            if e.get("warn"):
                self.log(f"urls.txt: {e['warn']}")
            if e["kind"] == "video":
                added += self.enqueue(e["id"], e["url"])
            elif e["kind"] == "playlist":
                if e["id"] not in self.playlists:
                    self.playlists[e["id"]] = {"id": e["id"], "url": e["url"], "title": "",
                                               "video_ids": [], "refreshed": 0}
        # playlists no longer in urls.txt: forget them. Their videos lose the playlist mark, so they
        # become cache (deleted when TD exits) unless another playlist or a urls.txt line keeps them
        listed = {e["id"] for e in entries if e["kind"] == "playlist"}
        for pid in [p for p in self.playlists if p not in listed]:
            pl = self.playlists.pop(pid)
            self.pending_confirm.pop(pid, None)
            n = 0
            for vid, m in list(self.metas.items()):
                pls = m.get("playlists") or []
                if any(p.get("id") == pid for p in pls):
                    self._write_meta(vid, playlists=[p for p in pls if p.get("id") != pid])
                    n += 1
            self.log(f"playlist '{pl.get('title') or pid}' is no longer in urls.txt: "
                     f"{n} video(s) are cache now (deleted when TD exits)")
        # only playlists that are new or were last read > 30 min ago (playlists.json keeps the
        # list across restarts, so a restart or a urls.txt save does not re-read every playlist)
        self.refresh_playlists(stale_only=not force)
        if added:
            self.log(f"urls.txt: queued {added} new video(s)")
        self.write_index()

    def refresh_playlists(self, only=None, stale_only=False):
        self._last_pl_refresh = time.time()
        for pid, pl in list(self.playlists.items()):
            if only and pid != only:
                continue
            if stale_only and time.time() - float(pl.get("refreshed") or 0) < self.PLAYLIST_REFRESH_S:
                continue
            try:
                self._expand_playlist(pl)
            except Exception as e:  # noqa
                self.log(f"playlist {pid}: {e}")
        self.write_index()

    def _expand_playlist(self, pl, confirmed=False):
        d = self.yt.json(["--flat-playlist", pl["url"]])
        pl["title"] = d.get("title") or pl.get("title", "")
        entries = [e for e in (d.get("entries") or []) if e]
        new = [e for e in entries if e.get("id") not in pl.get("video_ids", [])
               and e.get("id") not in self.metas]
        if len(new) > self.CONFIRM_LIMIT and not confirmed and not pl.get("confirmed"):
            self.pending_confirm[pl["id"]] = True
            self.log(f"playlist {pl['title']}: {len(new)} new videos, waiting for confirmation")
            self.on_confirm(pl["id"], pl["title"], len(new))
            return
        ids = []
        for i, e in enumerate(entries, 1):
            vid = e.get("id")
            if not vid:
                continue
            ids.append(vid)
            ref = {"id": pl["id"], "title": pl["title"], "index": i}
            title = e.get("title") or vid
            if title in ("[Private video]", "[Deleted video]") or e.get("availability") in (
                    "private", "needs_auth", "subscriber_only"):
                self._write_meta(vid, title=title, status="unavailable",
                                 reason=f"playlist entry: {title} / {e.get('availability')}",
                                 source="playlist")
                m = self.metas[vid]
                m["playlists"] = [p for p in m.get("playlists", []) if p["id"] != pl["id"]] + [ref]
                self._write_meta(vid, playlists=m["playlists"])
                continue
            self.enqueue(vid, f"https://www.youtube.com/watch?v={vid}", source="playlist",
                         playlist=ref, title=title)
        # keep removed videos (spec: never delete from library)
        pl["video_ids"] = ids + [v for v in pl.get("video_ids", []) if v not in ids]
        pl["refreshed"] = time.time()
        self.log(f"playlist '{pl['title']}': {len(entries)} entries, {len(new)} new")

    def confirm_reply(self, pid, ok):
        self.pending_confirm.pop(pid, None)
        pl = self.playlists.get(pid)
        if not pl:
            return
        if ok:
            pl["confirmed"] = True
            self._expand_playlist(pl, confirmed=True)
        else:
            self.log(f"playlist {pid}: import declined")
        self.write_index()

    # -------------------------------------------------------------- ingest
    def ingest(self, vid, url=None):
        url = url or f"https://www.youtube.com/watch?v={vid}"
        d = self.libdir / vid
        d.mkdir(parents=True, exist_ok=True)
        try:
            if vid not in self.upgrade:
                self._write_meta(vid, status="downloading")
                self.write_index()
            info = self.yt.json(["--no-playlist", url])
            self._write_meta(vid, title=info.get("title"), channel=info.get("channel") or info.get("uploader"),
                             duration=info.get("duration"), fps=info.get("fps"))
            if info.get("is_live") or info.get("live_status") in ("is_live", "is_upcoming", "post_live"):
                # a live stream would be recorded forever and has no fixed timeline to sync to
                (d / "video.mp4").unlink(missing_ok=True)
                self._write_meta(vid, status="unavailable",
                                 reason=f"live stream ({info.get('live_status') or 'is_live'}) - skipped")
                self.log(f"skipped: {vid} is a live stream")
                self.write_index()
                return
            out = d / "video.mp4"
            upgrade = vid in self.upgrade
            if upgrade or not out.exists():
                # download under a temporary name: a killed download (worker stop / restart) must
                # not leave a half file named video.mp4 that later looks complete
                for old in d.glob("dl.*"):
                    old.unlink(missing_ok=True)
                tmpl = str(d / "dl.%(ext)s")
                try:
                    self.yt.run(self._format_args() + ["--no-playlist", "--no-part", "-o", tmpl, url],
                                timeout=1800)
                except RuntimeError as e:
                    self.log(f"{vid}: {self.quality}p format unavailable ({e}); trying fallback")
                    self.yt.run(["-f", "b[height<=480][ext=mp4]/bv*[height<=480][vcodec^=avc1]+ba[ext=m4a]/"
                                 "bv*[height<=480]+ba/b", "--no-playlist", "--no-part",
                                 "--merge-output-format", "mp4", "--ffmpeg-location",
                                 shutil.which(self.ffmpeg) or self.ffmpeg, "-o", tmpl, url], timeout=1800)
                got = [p for p in d.glob("dl.*") if p.suffix not in (".part", ".ytdl", ".temp")]
                if not got:
                    raise RuntimeError("download produced no file")
                os.replace(got[0], out)     # (an open file in TD keeps playing the old one)
                self._write_meta(vid, height=self.video_height(out))
                if upgrade:
                    self.upgrade.discard(vid)
                    self.log(f"re-downloaded: {vid} at {self.metas[vid].get('height')}p")
            if not upgrade:
                self._write_meta(vid, status="analyzing")
            ref = d / "ref.npz"
            onset = F.load_ref(ref) if ref.exists() else None
            if onset is None:
                onset = self.analyze(vid)
            if self.metas.get(vid, {}).get("status") == "removed":   # removed while ingesting
                self.remove(vid)
                return False
            self._write_meta(vid, status="ready", reason=None, ready_at=now_iso())
            self.write_index()
            self.on_ready(vid, onset)
            self.log(f"ready: {vid} {self.metas[vid].get('title')}")
            return True
        except Exception as e:  # noqa
            msg = str(e)
            status = "unavailable" if any(s in msg for s in (
                "Private video", "Video unavailable", "not available in your country",
                "removed", "members-only", "Sign in to confirm your age")) else "failed"
            self._write_meta(vid, status=status, reason=msg[:2000])
            self.write_index()
            self.log(f"{status}: {vid}: {msg}")
            return False

    def _format_args(self):
        h = int(self.quality)
        if h <= 360:
            return ["-f", "18"]
        # H.264 only: TD's Movie File In decodes it in hardware; VP9 / AV1 are not safe there.
        # YouTube serves 720p+ as separate video / audio streams -> merged into one mp4 by ffmpeg
        return ["-f", f"bv*[height<={h}][vcodec^=avc1]+ba[ext=m4a]/b[height<={h}][ext=mp4]/18",
                "--merge-output-format", "mp4", "--ffmpeg-location", shutil.which(self.ffmpeg) or self.ffmpeg]

    def video_height(self, path):
        probe = shutil.which("ffprobe") or str(Path(shutil.which(self.ffmpeg) or self.ffmpeg).with_name("ffprobe"))
        try:
            r = subprocess.run([probe, "-v", "error", "-select_streams", "v:0", "-show_entries",
                                "stream=width,height", "-of", "csv=p=0", str(path)],
                               capture_output=True, text=True, timeout=30)
            w, h = (int(x) for x in r.stdout.strip().splitlines()[0].split(",")[:2])
            # "720p" class: 1280x536 (cinema) or 960x720 (4:3) both count as 720
            return max(h, round(w * 9 / 16))
        except Exception:
            return 0

    def upgrade_all(self):
        """Re-download every ready MV that is below the current quality (features are kept)."""
        n = 0
        for vid, m in list(self.metas.items()):
            if m.get("status") != "ready" or vid in self.upgrade:
                continue
            h = self.video_height(self.libdir / vid / "video.mp4")
            if h and h != m.get("height"):
                self._write_meta(vid, height=h)
            if h and h < int(self.quality):
                with self.lock:
                    self.upgrade.add(vid)
                    self.queued.add(vid)
                self.q.put((2, time.time(), vid, None))     # after normal downloads
                n += 1
        self.log(f"re-download at {self.quality}p: {n} video(s) queued")
        return n

    def worker_loop(self):
        """Ingest one by one (urls.txt is watched by watch_loop)."""
        while not self._stop.is_set():
            try:
                _, _, vid, url = self.q.get(timeout=0.5)
            except queue.Empty:
                continue
            with self.lock:
                if vid not in self.queued:      # duplicate of an entry that was moved ahead
                    continue
                self.queued.discard(vid)
            self.ingest(vid, url)

    def start(self):
        # resume what was still pending when the worker last stopped
        with self.lock:
            pending = [v for v, m in self.metas.items()
                       if m.get("status") in ("queued", "downloading", "analyzing")]
        for v in pending:
            self._write_meta(v, status="queued")   # was cut off mid-ingest last time
            self.enqueue(v)
        if pending:
            self.log(f"library: resuming {len(pending)} pending video(s)")
        th = threading.Thread(target=self.worker_loop, name="library", daemon=True)
        th.start()
        threading.Thread(target=self.watch_loop, name="urls", daemon=True).start()
        return th

    def watch_loop(self):
        """urls.txt / playlists, separate from downloads so a long download never delays them."""
        while not self._stop.is_set():
            try:
                self.check_urls()
            except Exception as e:  # noqa
                self.log(f"urls.txt check failed: {e}")
            self._stop.wait(2.0)

    def stop(self):
        self._stop.set()

    # -------------------------------------------------------------- auto (online) additions
    def append_auto_line(self, artist, title, vid):
        line = f"# auto: {artist} - {title}\nhttps://youtu.be/{vid}\n"
        with open(self.urls, "a", encoding="utf-8") as f:
            f.write(("\n" if self.urls.read_text(encoding="utf-8").endswith("\n") is False else "") + line)

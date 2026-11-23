"""MVSync worker: runs outside TouchDesigner (spec §2).

  python mvsync_worker.py --config ~/MVSync/config.json
  python mvsync_worker.py --config ... --input-file test.wav     # file instead of BlackHole
  python mvsync_worker.py --config ... --update-tools            # pip -U shazamio / yt-dlp
"""
import argparse
import csv
import json
import os
import signal
import subprocess
import sys
import threading
import time
from collections import deque
from datetime import datetime
from pathlib import Path

import numpy as np

HERE = Path(__file__).resolve().parent
sys.path.insert(0, str(HERE))

import mvsync_features as F  # noqa: E402
from mvsync_library import Library, YtDlp  # noqa: E402
from mvsync_match import (Engine, EngineCfg, RefBank, SEARCHING,  # noqa: E402
                          IDLE, LOCKED, Estimate)
from mvsync_online import OnlineResolver, set_title_words  # noqa: E402
import title_words as TW  # noqa: E402

IS_WIN = os.name == "nt"

DEFAULT_CONFIG = {
    "library_dir": "~/MVSync",
    "ytdlp": "",                   # empty = yt-dlp next to this Python (venv), then PATH
    "ytdlp_args": [],               # --js-runtimes is added automatically (bundled Deno, else node)
    "ytdlp_python": "",
    "node_dir": "",                # empty = node on PATH, /opt/homebrew/bin, /usr/local/bin
    "ffmpeg": "ffmpeg",
    "python": "",
    "audio_device": "",            # empty = the OS default input (Windows: recording default, macOS: Sound > Input)
    "osc_to_td": "127.0.0.1:9100",
    "osc_from_td": 9101,
    "recognizer": "shazamio",
    "audd_token": "",
    "shazam_language": "ja-JP",    # Shazam answers in this language (titles as in Japanese stores)
    "shazam_country": "JP",
    "youtube_lang": "ja",          # YouTube titles in this language (search / metadata)
    "online_fallback": True,
    "theta_lock": 0.6,
    "theta_hold": 0.45,
    "theta_margin": 0.03,
    "online_early_s": 2.0,         # MV lost this long -> ask Shazam (library matching keeps going)
    "online_retry_s": 15.0,        # next try after "not recognised" (max 3 per lost period)
    "online_quick_retry_s": 4.0,   # next try when Shazam still heard the previous song (MV on screen)
    "online_provisional": True,    # show an online MV at its verify position before LOCKED
    "online_prefetch_url": True,   # fetch the direct URL during the audio check, not after it
    "remix_original": True,        # Shazam names a remix -> the original song's MV from the top (state REMIX)
    "remix_retry_s": 20.0,         # Shazam interval while a remix plays (no position sync to notice the next song)
    "remix_words": ["remix", "bootleg", "flip", "vip", "edit", "rework", "mashup", "re-edit", "refix", "remake"],
    "remix_exclude": ["radio edit", "tv edit", "single edit", "album edit", "short edit", "original mix",
                      "extended mix", "extended", "tv size", "clean", "explicit", "remaster"],
    "title_priority_words": list(TW.PRIORITY_WORDS),   # online search: these in a title rank first (TD: Priority words)
    "title_ng_words": list(TW.NG_WORDS),               # online search: never these videos (TD: NG words)
    "theta_verify": 0.7,
    "stall_reset_s": 5.0,          # Confidence unchanged this long -> reset and search again (0 = off)
    "video_quality": 720,          # max height to download: 360 (format 18), 720 or 1080 (H.264 + merge)
    "official_only": True,         # online: adopt only official uploads (verified / official / artist channel)
    "rate_range": [0.84, 1.16],    # playback speed searched (±16 %)
    "score_log": True,
    "playlist_order_bonus": 0.0,
}



def load_config(path):
    path = Path(os.path.expanduser(path))
    cfg = dict(DEFAULT_CONFIG)
    if path.exists():
        try:
            cfg.update(json.loads(path.read_text(encoding="utf-8")))
        except Exception as e:  # noqa
            print(f"config read failed ({e}); using defaults", file=sys.stderr)
    else:
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(json.dumps(cfg, indent=1, ensure_ascii=False), encoding="utf-8")
    return cfg, path


def find_ytdlp(cfg):
    import shutil
    exe = os.path.expanduser(cfg.get("ytdlp") or "")
    if exe:
        return exe
    cand = Path(sys.executable).with_name("yt-dlp.exe" if IS_WIN else "yt-dlp")
    if cand.exists():
        return str(cand)
    return shutil.which("yt-dlp") or "yt-dlp"


def find_node_dir(cfg):
    import shutil
    d = os.path.expanduser(cfg.get("node_dir") or "")
    if d:
        return d
    found = shutil.which("node")
    for cand in ([os.path.dirname(found)] if found else []) + ["/opt/homebrew/bin", "/usr/local/bin"]:
        if os.path.exists(os.path.join(cand, "node.exe" if IS_WIN else "node")):
            return cand
    return None


def js_runtime_args(cfg, args):
    """yt-dlp needs a JS runtime for YouTube: the Deno from the venv (pip package "deno"), else node."""
    if any(str(a).startswith("--js-runtimes") for a in args):
        return []
    try:
        import deno
        exe = deno.find_deno_bin()
        if exe and os.path.exists(exe):
            return ["--js-runtimes", "deno:" + exe]
    except Exception:
        pass
    if find_node_dir(cfg):
        return ["--js-runtimes", "node"]
    return []


def find_ffmpeg(cfg):
    import shutil
    exe = cfg.get("ffmpeg") or "ffmpeg"
    if os.path.isabs(os.path.expanduser(exe)):
        return os.path.expanduser(exe)
    found = shutil.which(exe)
    for cand in ([found] if found else []) + ["/opt/homebrew/bin/ffmpeg", "/usr/local/bin/ffmpeg"]:
        if cand and os.path.exists(cand):
            return cand
    try:
        import imageio_ffmpeg            # bundled ffmpeg (pip package "imageio-ffmpeg")
        return imageio_ffmpeg.get_ffmpeg_exe()
    except Exception:
        return exe


# ===================================================================== audio input
class AudioRing:
    """Mono ring buffer at the device rate with a sample -> epoch-time mapping."""

    def __init__(self, sr, seconds=20.0):
        self.sr = int(sr)
        self.buf = np.zeros(int(seconds * self.sr), np.float32)
        self.n = 0                       # total samples written
        self.lock = threading.Lock()
        self._base = deque()             # (t_wall, base_estimate)

    def push(self, mono, t_wall=None):
        t_wall = time.time() if t_wall is None else t_wall
        with self.lock:
            k = len(mono)
            L = len(self.buf)
            i = self.n % L
            if k >= L:
                self.buf[:] = mono[-L:]
            else:
                j = min(k, L - i)
                self.buf[i:i + j] = mono[:j]
                self.buf[:k - j] = mono[j:]
            self.n += k
            # epoch time of sample 0 = t_wall - n/sr ; callbacks arrive late -> take min
            self._base.append((t_wall, t_wall - self.n / self.sr))
            while self._base and t_wall - self._base[0][0] > 30.0:
                self._base.popleft()

    def base(self):
        return min(b for _, b in self._base) if self._base else time.time()

    def last(self, seconds):
        """Return (samples, t_end) for the last `seconds`."""
        with self.lock:
            k = min(int(seconds * self.sr), self.n, len(self.buf))
            L = len(self.buf)
            i = self.n % L
            idx = (np.arange(i - k, i)) % L
            y = self.buf[idx].copy()
            t_end = self.base() + self.n / self.sr
        return y, t_end


class AudioDeviceError(RuntimeError):
    pass


# Windows lists every device once per host API; lower latency first (WDM-KS is left out: flaky)
HOSTAPI_ORDER = ["Windows WASAPI", "Windows DirectSound", "MME", "Core Audio", "ALSA", "JACK Audio Connection Kit"]


class DeviceInput:
    def __init__(self, name, log):
        import sounddevice as sd
        self.sd = sd
        devs = sd.query_devices()
        apis = [a["name"] for a in sd.query_hostapis()]
        name = (name or "").strip()
        if name:
            cands = [i for i, d in enumerate(devs) if d["max_input_channels"] > 0 and name.lower() in d["name"].lower()]
        else:
            # the OS default input; on Windows every host API has its own default -> try WASAPI's first
            cands = [h["default_input_device"] for h in sd.query_hostapis()
                     if h.get("default_input_device", -1) is not None and h.get("default_input_device", -1) >= 0
                     and devs[h["default_input_device"]]["max_input_channels"] > 0]
            if not cands and sd.default.device[0] is not None and sd.default.device[0] >= 0:
                cands = [sd.default.device[0]]
            name = "(OS の既定の入力)"

        def rank(i):
            api = apis[devs[i]["hostapi"]]
            return HOSTAPI_ORDER.index(api) if api in HOSTAPI_ORDER else len(HOSTAPI_ORDER)
        cands = [i for i in sorted(cands, key=rank) if apis[devs[i]["hostapi"]] != "Windows WDM-KS"] or cands
        if not cands:
            names = sorted({d["name"] for d in devs if d["max_input_channels"] > 0})
            raise AudioDeviceError("音声入力デバイス「%s」が見つかりません。TD の Config ページの Audio device に、"
                                   "次のどれかの名前の一部を入れてください: %s" % (name, " / ".join(names)))

        def cb(indata, frames, tinfo, status):
            self.ring.push(indata.mean(axis=1).astype(np.float32))

        errors = []
        for idx in cands:
            d = devs[idx]
            sr = int(d["default_samplerate"])
            ch = min(2, int(d["max_input_channels"]))
            try:
                self.ring = AudioRing(sr)
                self.stream = sd.InputStream(device=idx, channels=ch, samplerate=sr,
                                             blocksize=1024, dtype="float32", callback=cb)
            except Exception as e:  # noqa
                errors.append(f"{d['name']} [{apis[d['hostapi']]}]: {e}")
                continue
            self.sr, self.ch, self.name = sr, ch, d["name"]
            log(f"audio: {self.name} [{apis[d['hostapi']]}] @ {self.sr} Hz, {self.ch} ch")
            return
        raise AudioDeviceError("音声入力デバイスを開けません: " + " | ".join(errors))

    def start(self):
        self.stream.start()

    def stop(self):
        try:
            self.stream.stop()
            self.stream.close()
        except Exception:
            pass


class FileInput:
    """Plays a media file into the ring in real time (for --input-file)."""

    def __init__(self, path, ffmpeg, log, loop=False):
        self.log = log
        self.y = F.decode_audio(path, ffmpeg, sr=F.SR)
        self.sr = F.SR
        self.ring = AudioRing(self.sr)
        self.loop = loop
        self._stop = threading.Event()
        self.done = threading.Event()
        log(f"file input: {path} ({len(self.y) / self.sr:.1f}s)")

    def _run(self):
        blk = 1024
        t_start = time.time()
        # exact wall time of file position 0 (evaluation maps log times back to the file)
        self.log(f"file input start t0={t_start:.3f}")
        i = 0
        while not self._stop.is_set():
            if i >= len(self.y):
                if not self.loop:
                    self.done.set()
                    return
                i = 0
            target = t_start + (i + blk) / self.sr
            dt = target - time.time()
            if dt > 0:
                time.sleep(dt)
            self.ring.push(self.y[i:i + blk])
            i += blk

    def start(self):
        threading.Thread(target=self._run, daemon=True).start()

    def stop(self):
        self._stop.set()


# ===================================================================== worker
class Worker:
    TICK = 0.5          # tracking period
    QUERY_S = 8.0
    CONTEXT_S = 12.0

    def __init__(self, cfg, cfg_path, input_file=None, no_online=False):
        self.cfg = cfg
        self.cfg_path = cfg_path
        self.root = Path(os.path.expanduser(cfg["library_dir"]))
        (self.root / "logs").mkdir(parents=True, exist_ok=True)
        self.logf = open(self.root / "logs" / "worker.log", "a", encoding="utf-8")
        self.ffmpeg = find_ffmpeg(cfg)
        self._stop = threading.Event()
        self.lock = threading.RLock()
        from pythonosc.udp_client import SimpleUDPClient
        host, port = cfg["osc_to_td"].rsplit(":", 1)
        self.osc = SimpleUDPClient(host, int(port))
        yargs = list(cfg.get("ytdlp_args") or [])
        yargs += js_runtime_args(cfg, yargs)
        if cfg.get("youtube_lang", "ja"):
            # titles in Japanese: in English YouTube returns "ftrygry - Swallow" for 「燕」 and the
            # candidate check (title must name the song) dropped the right MV
            yargs += ["--extractor-args", "youtube:lang=%s" % cfg.get("youtube_lang", "ja")]
        self.yt = YtDlp(find_ytdlp(cfg), yargs, find_node_dir(cfg), log=self.log)
        self.bank = RefBank()
        ecfg = EngineCfg(theta_lock=float(cfg["theta_lock"]), theta_hold=float(cfg["theta_hold"]),
                         theta_margin=float(cfg.get("theta_margin", 0.03)),
                         rate_range=tuple(cfg["rate_range"]),
                         stall_reset_s=float(cfg.get("stall_reset_s", 5.0)))
        self.engine = Engine(self.bank, ecfg, logger=self.log)
        self.lib = Library(self.root, self.yt, self.ffmpeg, log=self.log,
                           on_change=lambda p: self.osc_send("/mvsync/library", [p]),
                           on_ready=self._on_ready,
                           on_confirm=lambda pid, t, n: self.osc_send("/mvsync/confirm", [pid, t, n]))
        self.lib.quality = int(cfg.get("video_quality", 720))
        self.online = OnlineResolver(self)
        set_title_words(cfg.get("title_priority_words", TW.PRIORITY_WORDS),
                        cfg.get("title_ng_words", TW.NG_WORDS))
        self.no_online = no_online
        self.input_file = input_file
        self.input = None
        self.sync_on = True
        self.overlay_state = None
        self.remix = None          # {"vid","key","dur","title"}: original MV played for a remix
        self.no_mv_song = ""       # recognised song with no MV: shown as NO_MV until a MV locks
        self.sent_state = None
        self.playing_id = None
        self.playing_src = None
        self.search_since = None
        self.silent_flag = False
        self._feat = (None, None, None)
        self.csv = None
        self._open_csv()

    # ------------------------------------------------------------- io helpers
    def log(self, msg):
        line = f"{datetime.now().strftime('%H:%M:%S')} {msg}"
        print(line, flush=True)
        try:
            self.logf.write(line + "\n")
            self.logf.flush()
        except Exception:
            pass
        if any(k in msg for k in ("ERROR", "failed", "失敗", "error", "not found", "warn",
                                  "ready:", "adopted", "recognised", "state", "playlist", "urls.txt",
                                  "version", "unavailable", "jump", "audio:")):
            self.osc_send("/mvsync/log", [msg[:4000]])

    def osc_send(self, addr, args):
        try:
            from pythonosc.osc_message_builder import OscMessageBuilder
            b = OscMessageBuilder(address=addr)
            for a in args:
                if isinstance(a, float) and addr in ("/mvsync/heartbeat", "/mvsync/est") and a > 1e6:
                    b.add_arg(a, "d")
                elif isinstance(a, bool):
                    b.add_arg(int(a), "i")
                else:
                    b.add_arg(a)
            self.osc.send(b.build())
        except Exception as e:  # noqa
            print(f"osc send failed {addr}: {e}", file=sys.stderr)

    def _open_csv(self):
        if not self.cfg.get("score_log", True):
            self.csv = None
            return
        p = self.root / "logs" / f"scores_{datetime.now():%Y%m%d_%H%M%S}.csv"
        f = open(p, "w", newline="", encoding="utf-8")
        self.csv = (f, csv.writer(f))
        self.csv[1].writerow(["t", "kind", "state", "rank", "id", "pos", "r", "score", "avg4",
                              "n", "seen", "t0", "margin", "decision", "est_id", "est_pos"])

    def _csv_rows(self, rows):
        if self.csv:
            self.csv[1].writerows(rows)
            self.csv[0].flush()

    # ------------------------------------------------------------- callbacks for online
    def set_overlay_state(self, s):
        self.overlay_state = s
        self._send_state()

    def get_audio(self, seconds):
        if not self.input:
            return None
        y, _ = self.input.ring.last(seconds)
        return F.resample(y, self.input.ring.sr, F.SR)

    def current_query(self):
        return self._feat

    def add_ref(self, vid, onset):
        with self.lock:
            self.bank.add(vid, onset)

    def start_remix(self, vid, key, dur, title=""):
        """Remix recognised: play the original song's MV from the top at rate 1 (not LOCKED)."""
        with self.lock:
            eng = self.engine
            if eng.state == LOCKED or eng.manual_id:
                return False
            if eng.state != SEARCHING:
                eng.relock()
            eng.prov_id = None
            now = time.time()
            eng.est = Estimate(vid, 0.0, 1.0, 0.0, now)
            eng.track_changed = False
            self.remix = {"vid": vid, "key": key, "dur": float(dur or 0), "title": title}
            self.no_mv_song = ""
            self._send_play(vid, force=True)
            self.osc_send("/mvsync/track_changed", [vid])
            self._send_state()
        return True

    def stop_remix(self, why):
        with self.lock:
            if self.remix:
                self.log(f"remix ended: {why}")
                self.remix = None
                self._send_state()

    def _remix_tick(self, now):
        """Loop the original MV; end remix mode when the library locks or shows another MV."""
        rm, eng = self.remix, self.engine
        if not rm:
            return
        if eng.state == LOCKED or eng.est.id != rm["vid"]:
            self.remix = None
            self.log(f"remix ended: library matching took over ({eng.state} {eng.est.id})")
            return
        if rm["dur"] > 1.0 and eng.est.pos_at(now) >= rm["dur"] - 0.5:
            eng.est = Estimate(rm["vid"], 0.0, eng.est.rate, 0.0, now)     # back to the top

    def show_provisional(self, cand):
        """Online MV verified: play it now at the verify position (todo_20261114 #1)."""
        with self.lock:
            if not self.engine.provisional(cand):
                return
            self.engine.track_changed = False
            self._send_play(cand.id)
            self.osc_send("/mvsync/track_changed", [cand.id])
            self._send_state()

    def _on_ready(self, vid, onset):
        with self.lock:
            self.bank.add(vid, onset)
            self.online.stream_urls.pop(vid, None)
            if self.playing_id == vid:
                self._send_play(vid, force=True)

    # ------------------------------------------------------------- play / state
    def play_source(self, vid):
        f = self.lib.video_file(vid)
        if os.path.exists(f) and (self.lib.meta(vid) or {}).get("status") == "ready":
            return f
        return self.online.stream_urls.get(vid) or (f if os.path.exists(f) else "")

    def _send_play(self, vid, force=False):
        src = self.play_source(vid)
        if force or vid != self.playing_id or src != self.playing_src:
            self.playing_id, self.playing_src = vid, src
            self.osc_send("/mvsync/play", [vid, src])
            on = self.bank.refs.get(vid)
            if on is not None:
                self.osc_send("/mvsync/bpm", [vid, float(F.estimate_bpm(on))])

    def _send_state(self):
        s = self.overlay_state or self.engine.state
        eng = self.engine
        if not self.overlay_state and eng.state == SEARCHING and eng.prov_id and eng.est.id == eng.prov_id:
            s = "PROVISIONAL"      # online MV at the verify position, not LOCKED yet
        if not self.overlay_state and self.remix and eng.state != LOCKED:
            s = "REMIX"            # the original song's MV for a remix, from the top (not synced)
        if not self.overlay_state and self.no_mv_song:
            if self.engine.state == LOCKED:
                self.no_mv_song = ""
            elif self.engine.state != IDLE:
                s = "NO_MV"
        if s != self.sent_state:
            self.sent_state = s
            self.osc_send("/mvsync/state", [s])

    def resend(self):
        self.sent_state = None
        self._send_state()
        self.osc_send("/mvsync/library", [str(self.lib.index_path)])
        # TD may still hold the previous worker's silence flag (it showed black although LOCKED)
        self.osc_send("/mvsync/silence", [1 if self.silent_flag else 0])
        if self.playing_id:
            self._send_play(self.playing_id, force=True)
        self.osc_send("/mvsync/versions", [self.yt.version(), self._shazam_version()])

    @staticmethod
    def _shazam_version():
        try:
            from importlib.metadata import version
            return version("shazamio")
        except Exception:
            return "unavailable"

    # ------------------------------------------------------------- OSC commands
    def _osc_server(self):
        from pythonosc.dispatcher import Dispatcher
        from pythonosc.osc_server import ThreadingOSCUDPServer
        d = Dispatcher()

        def h(fn):
            def wrap(addr, *args):
                try:
                    fn(*args)
                except Exception as e:  # noqa
                    self.log(f"command {addr} failed: {e}")
            return wrap

        d.map("/mvsync/relock", h(lambda *a: self._cmd_relock()))
        d.map("/mvsync/reject", h(lambda *a: self._cmd_reject()))
        d.map("/mvsync/select", h(lambda vid="", *a: self._cmd_select(vid)))
        d.map("/mvsync/sync", h(lambda v=1, *a: setattr(self, "sync_on", bool(int(v)))))
        d.map("/mvsync/retry", h(lambda vid, *a: self.lib.retry(vid)))
        d.map("/mvsync/remove", h(lambda vid, *a: self._cmd_remove(vid)))
        d.map("/mvsync/upgrade_videos", h(lambda *a: threading.Thread(
            target=self.lib.upgrade_all, daemon=True).start()))
        d.map("/mvsync/param", h(lambda n, v, *a: self._cmd_param(n, v)))
        d.map("/mvsync/mark", h(lambda v, *a: self._cmd_mark(v)))
        d.map("/mvsync/active_playlist", h(lambda pid, *a: self._cmd_playlist(pid)))
        d.map("/mvsync/refresh_playlists", h(lambda *a: threading.Thread(
            target=self.lib.check_urls, kwargs={"force": True}, daemon=True).start()))
        d.map("/mvsync/confirm_reply", h(lambda pid, ok, *a: threading.Thread(
            target=self.lib.confirm_reply, args=(pid, bool(int(ok))), daemon=True).start()))
        d.map("/mvsync/resend", h(lambda *a: self.resend()))
        d.map("/mvsync/quit", h(lambda *a: (self.log("quit requested by TD"), self.stop())))
        srv = ThreadingOSCUDPServer(("127.0.0.1", int(self.cfg["osc_from_td"])), d)
        self.osc_srv = srv
        threading.Thread(target=srv.serve_forever, daemon=True, name="osc").start()

    def _cmd_relock(self):
        with self.lock:
            self.engine.relock()
            self.search_since = None
            self.online.restart()
        self.log("relock requested")

    def _cmd_reject(self):
        with self.lock:
            vid = self.engine.est.id
            if not vid:
                return
            is_remix = bool(self.remix and self.remix["vid"] == vid)
            # a remix: rejected under the remix's own name (a re-check may be running right now)
            self.online.reject(vid, self.remix["key"] if is_remix else None)
            if is_remix:
                # only this remix stops using it: the MV stays usable for the original song itself
                self.remix = None
                self.online.restart()
            else:
                self.engine.excluded.add(vid)
            if self.engine.manual_id == vid:
                self.engine.manual_id = None
            self.engine.est.id = ""
            self.engine.relock()
            self.online.reset_attempts()
        self.log(f"rejected {vid}")

    def _cmd_select(self, vid):
        with self.lock:
            vid = vid or ""
            self.engine.manual_id = vid or None
            if vid:
                self.engine.excluded.discard(vid)
            self.engine.relock()
            if vid:
                self._send_play(vid, force=True)
        self.log(f"manual select: {vid or '(auto)'}")

    def _cmd_remove(self, vid):
        with self.lock:
            self.bank.remove(vid)
            self.online.stream_urls.pop(vid, None)
            eng = self.engine
            if eng.manual_id == vid:
                eng.manual_id = None
            if eng.est.id == vid:            # it is on screen: drop it and search again
                eng.relock()
                eng.est = type(eng.est)()
                self.playing_id = None
                self.osc_send("/mvsync/unload", [vid])
        self.lib.remove(vid)

    def _cmd_param(self, name, value):
        with self.lock:
            if name in ("theta_lock", "theta_hold", "theta_margin"):
                self.engine.set_param(name, float(value))
                self.cfg[name] = float(value)
            elif name in ("online_fallback", "score_log", "official_only"):
                self.cfg[name] = bool(int(float(value)))
                if name == "score_log" and self.cfg[name] and not self.csv:
                    self._open_csv()
            elif name in ("playlist_order_bonus", "theta_verify", "online_early_s", "online_retry_s"):
                self.cfg[name] = float(value)
            elif name == "video_quality":
                self.cfg[name] = int(float(value))
                self.lib.quality = int(float(value))
                self.log(f"video quality: {self.lib.quality}p (new downloads)")
            elif name == "rate_range_pct":
                x = float(value) / 100.0
                self.cfg["rate_range"] = [round(1 - x, 4), round(1 + x, 4)]
                self.engine.cfg.rate_range = tuple(self.cfg["rate_range"])
                self.log(f"speed range: ±{float(value):.0f} %")
            elif name in ("title_priority_words", "title_ng_words"):
                words = TW.parse(str(value))
                self.cfg[name] = words
                if name == "title_priority_words":
                    set_title_words(priority=words)
                else:
                    set_title_words(ng=words)
                self.log(f"{name}: {len(words)} words")
                return
            elif name == "stall_reset_s":
                self.cfg[name] = float(value)
                self.engine.set_param(name, float(value))
            else:
                self.log(f"unknown param {name}")
                return
        self.log(f"param {name} = {value}")

    def _cmd_mark(self, v):
        e = self.engine.est
        t = time.time()
        self._csv_rows([[f"{t:.3f}", "mark", self.engine.state, "", e.id,
                         f"{e.pos_at(t):.3f}", f"{e.rate:.4f}", "", "", "", "", "", "", str(v), e.id,
                         f"{e.pos_at(t):.3f}"]])
        self.log(f"mark {v}")

    def _cmd_playlist(self, pid):
        with self.lock:
            if not pid or pid == "All":
                self.engine.allowed_ids = None
            else:
                pl = self.lib.playlists.get(pid)
                self.engine.allowed_ids = set(pl.get("video_ids", [])) if pl else None
            self.engine.relock()
        self.log(f"active playlist: {pid}")

    # ------------------------------------------------------------- main loop
    def _features(self):
        ring = self.input.ring
        y, t_end = ring.last(self.CONTEXT_S)
        if len(y) < ring.sr * (self.QUERY_S + 1):
            return None, None, None, -120.0
        tail = y[-int(2.0 * ring.sr):]
        db = 20 * np.log10(np.sqrt(np.mean(tail * tail)) + 1e-12)
        y22 = F.resample(y, ring.sr, F.SR)
        q, qb = F.features_full(y22)
        n = int(self.QUERY_S * F.FPS)
        t0 = t_end - n / F.FPS
        return q[-n:], qb[-n:], t0, db

    def _estimate_loop(self):
        while not self._stop.is_set():
            e = self.engine.est
            if e.id:   # also while SEARCHING: stale = inertia, tentative = identified MV
                self.osc_send("/mvsync/est", [e.id, float(e.pos), float(e.rate),
                                              float(e.conf), float(e.t_ref)])
            time.sleep(0.1)

    def _heartbeat_loop(self):
        while not self._stop.is_set():
            self.osc_send("/mvsync/heartbeat", [float(time.time())])
            g = sorted(self.engine.last_global, key=lambda h: -h.avg(4))
            s1 = g[0].avg(4) if g else 0.0
            s2 = g[1].avg(4) if len(g) > 1 else 0.0
            id1 = g[0].id if g else ""
            self.osc_send("/mvsync/scores", [id1, float(s1), float(s2),
                                             float(s1 / s2) if s2 > 1e-6 else 0.0,
                                             float(self.engine.last_track_score)])
            time.sleep(1.0)

    def run(self):
        self.log(f"mvsync worker start (pid {os.getpid()}) python {sys.version.split()[0]}")
        self.log(f"version yt-dlp {self.yt.version()} / shazamio {self._shazam_version()}")
        self._osc_server()
        # library refs (may rebuild stale features)
        for vid, onset in self.lib.load_refs():
            self.bank.add(vid, onset)
        self.log(f"library: {len(self.bank.refs)} ready")
        self.lib.write_index()
        self.lib.start()
        if self.input_file:
            self.input = FileInput(self.input_file, self.ffmpeg, self.log)
        else:
            try:
                self.input = DeviceInput(self.cfg.get("audio_device", ""), self.log)
            except Exception as e:  # noqa
                self.log("ERROR " + str(e))      # also reaches TD (Last log) before the worker exits
                time.sleep(0.5)
                raise
        self.input.start()
        threading.Thread(target=self._estimate_loop, daemon=True).start()
        threading.Thread(target=self._heartbeat_loop, daemon=True).start()
        self.resend()
        k = 0
        next_t = time.time()
        silent_since = None
        while not self._stop.is_set():
            next_t += self.TICK
            dt = next_t - time.time()
            if dt > 0:
                time.sleep(dt)
            else:
                next_t = time.time()
            if isinstance(self.input, FileInput) and self.input.done.is_set():
                self.log("file input finished")
                break
            q, qb, t0, db = self._features()
            if q is None:
                continue
            self._feat = (q, qb, t0)
            now = time.time()
            with self.lock:
                eng = self.engine
                if db < -50.0:
                    silent_since = silent_since or now
                    if now - silent_since >= 2.0:
                        if not self.silent_flag:
                            self.silent_flag = True
                            self.osc_send("/mvsync/silence", [1])
                        eng.silent(now)
                        self.search_since = None
                        self._send_state()
                        continue
                else:
                    silent_since = None
                    if self.silent_flag:
                        self.silent_flag = False
                        self.osc_send("/mvsync/silence", [0])
                prev_id = eng.est.id
                eng.track_step(qb, t0)
                if k % 2 == 0:
                    t_a = time.time()
                    hyps, decision = eng.global_step(q, qb, t0)
                    el = time.time() - t_a
                    if eng.stall_check(now):
                        decision = (decision + " " if decision else "") + "stall-reset"
                        self.online.restart()
                    if el > 0.5:
                        self.log(f"warn: global search took {el * 1000:.0f} ms "
                                 f"({len(self.bank.refs)} refs)")
                    top = sorted(hyps, key=lambda h: -h.avg(4))[:5]
                    rows = [[f"{now:.3f}", "window", eng.state, i, h.id, f"{h.pos:.3f}", f"{h.r:.4f}",
                             f"{h.scores[-1]:.4f}", f"{h.avg(4):.4f}", len(h.scores), h.seen,
                             f"{h.t0:.3f}", f"{eng.last_margin:.4f}" if i == 0 else "",
                             decision if i == 0 else "", eng.est.id,
                             f"{eng.est.pos_at(h.t0):.3f}" if eng.est.id else ""]
                            for i, h in enumerate(top)]
                    self._csv_rows(rows or [[f"{now:.3f}", "window", eng.state, "", "", "", "", "",
                                             "", "", "", "", "", decision, eng.est.id, ""]])
                    # online lookup runs IN PARALLEL with library matching (spec §4.7 changed):
                    # once the MV has been lost for online_early_s, ask Shazam; the library wins if
                    # it locks first (lookup cancelled) or if the recognised song is in the library.
                    if eng.state != LOCKED:
                        if self.search_since is None:
                            self.search_since = now
                        known = decision.startswith("tentative")   # library already knows the song
                        if (self.cfg.get("online_fallback", True) and not self.no_online and self.sync_on
                                and not eng.manual_id and not known
                                and now - self.search_since >= float(self.cfg.get("online_early_s", 2.0))
                                and self.online.can_start(now, float(self.cfg.get("online_retry_s", 15.0)))):
                            self.log(f"online: no library lock for {now - self.search_since:.0f} s "
                                     f"-> recognising (library matching continues)")
                            self.online.start(now)
                    else:
                        if self.search_since is not None:
                            self.online.cancel("library locked")
                        self.search_since = None
                        self.online.reset_attempts()
                self._remix_tick(now)
                if eng.est.id and (eng.est.id != prev_id or eng.track_changed):
                    eng.track_changed = False
                    self._send_play(eng.est.id)
                    self.osc_send("/mvsync/track_changed", [eng.est.id])
                self._send_state()
            k += 1
        self.stop()

    def stop(self):
        if self._stop.is_set():
            return
        self._stop.set()
        self.log("worker stopping")
        try:
            self.lib.stop()
            if self.input:
                self.input.stop()
            self.osc_send("/mvsync/state", ["IDLE"])
            if hasattr(self, "osc_srv"):
                threading.Thread(target=self.osc_srv.shutdown, daemon=True).start()
        except Exception:
            pass


# ===================================================================== update tools
def update_tools(cfg):
    from pythonosc.udp_client import SimpleUDPClient
    host, port = cfg["osc_to_td"].rsplit(":", 1)
    osc = SimpleUDPClient(host, int(port))

    def say(msg):
        print(msg, flush=True)
        osc.send_message("/mvsync/log", [msg[:4000]])

    ytdlp = find_ytdlp(cfg)
    ypy = os.path.expanduser(cfg.get("ytdlp_python") or "")
    if not ypy:
        cand = Path(ytdlp).with_name("python.exe" if IS_WIN else "python")
        ypy = str(cand) if cand.exists() else ""
    cmds = [[sys.executable, "-m", "pip", "install", "-U", "shazamio"]]
    if ypy:
        cmds.append([ypy, "-m", "pip", "install", "-U", "yt-dlp[default]"])
    else:
        say("yt-dlp の python が見つかりません（config の ytdlp_python を設定してください）")
    for c in cmds:
        say("$ " + " ".join(c))
        p = subprocess.run(c, capture_output=True, text=True)
        tail = (p.stdout + p.stderr).strip().splitlines()[-6:]
        say("\n".join(tail) if tail else f"exit {p.returncode}")
    yv = YtDlp(ytdlp).version()
    try:
        sv = subprocess.run([sys.executable, "-c",
                             "from importlib.metadata import version;print(version('shazamio'))"],
                            capture_output=True, text=True).stdout.strip()
    except Exception:
        sv = "?"
    osc.send_message("/mvsync/versions", [yv, sv])
    say(f"version yt-dlp {yv} / shazamio {sv}")


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--config", default="~/MVSync/config.json")
    ap.add_argument("--input-file")
    ap.add_argument("--realtime", action="store_true",
                    help="feed --input-file at 1x speed (always the case; flag kept for clarity)")
    ap.add_argument("--no-online", action="store_true")
    ap.add_argument("--update-tools", action="store_true")
    a = ap.parse_args()
    cfg, path = load_config(a.config)
    if a.update_tools:
        update_tools(cfg)
        return
    w = Worker(cfg, path, input_file=a.input_file, no_online=a.no_online)
    signal.signal(signal.SIGTERM, lambda *x: (w.log("SIGTERM"), w.stop()))
    try:
        signal.signal(signal.SIGINT, lambda *x: (w.log("SIGINT"), w.stop()))
    except Exception:
        pass
    w.run()


if __name__ == "__main__":
    main()

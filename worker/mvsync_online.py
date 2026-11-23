"""MVSync online fallback (spec §4.7): recognise -> YouTube search -> verify -> ingest."""
import asyncio
import json
import logging
import re
import tempfile
import threading
import time
import unicodedata
import urllib.parse
import urllib.request
import wave
from pathlib import Path

import numpy as np

import mvsync_features as F
import title_words as TW
from mvsync_match import RefBank


class RecognizerError(Exception):
    """Service failure (429, changed API...). Distinct from 'not recognised'."""


class Recognizer:
    name = "base"

    def recognize(self, wav_path):  # -> {"artist":..., "title":...} | None
        raise NotImplementedError


class RecognizerTimeout(Exception):
    """The service did not answer in time (slow / throttled) - not a broken recognizer."""


class ShazamRecognizer(Recognizer):
    name = "shazamio"
    timeout = 10.0

    def __init__(self, language="ja-JP", country="JP"):
        # answers in this language: the default en-US / GB gives romaji or English titles for
        # Japanese songs ("AinoNiconico", "Super Suki Shuki Rush"), which YouTube searches miss
        self.language, self.country = language, country

    def recognize(self, wav_path):
        try:
            from shazamio import Shazam
        except Exception as e:  # noqa
            raise RecognizerError(f"shazamio import failed: {e!r}")

        async def run():
            sh = Shazam(language=self.language, endpoint_country=self.country)
            fn = getattr(sh, "recognize", None) or getattr(sh, "recognize_song")
            # Shazam sometimes takes 30 s+ (throttling); a lookup must never hang the pipeline
            return await asyncio.wait_for(fn(str(wav_path)), timeout=self.timeout)

        logging.getLogger("asyncio").setLevel(logging.CRITICAL)  # shazamio leaves sessions open
        try:
            out = asyncio.run(run())
        except asyncio.TimeoutError:
            raise RecognizerTimeout(f"no answer within {self.timeout:.0f} s")
        except Exception as e:  # noqa
            raise RecognizerError(f"{type(e).__name__}: {e}")
        if not isinstance(out, dict):
            raise RecognizerError(f"unexpected response: {str(out)[:300]}")
        if "track" not in out:
            if out.get("matches") == [] or "matches" in out:
                return None
            raise RecognizerError(f"unexpected response: {json.dumps(out)[:300]}")
        tr = out["track"]
        rec = {"artist": tr.get("subtitle", ""), "title": tr.get("title", "")}
        if tr.get("key") and needs_english_artist(rec["artist"], rec["title"]):
            en = self.english_artist(tr["key"])
            if en and en != rec["artist"]:
                rec["artist_en"] = en
        return rec

    def english_artist(self, key):
        """In ja-JP, Western artists come back in katakana ("ケンドリック・ラマー"): YouTube then finds
        Japanese fan uploads (【和訳】...) before the official video. Ask once more in en-US by the
        track key (~0.2 s)."""
        from shazamio import Shazam

        async def run():
            sh = Shazam(language="en-US", endpoint_country="US")
            return await asyncio.wait_for(sh.track_about(track_id=int(key)), timeout=5.0)
        try:
            r = asyncio.run(run())
            return (r or {}).get("subtitle", "") if isinstance(r, dict) else ""
        except Exception:  # noqa
            return ""


class AudDRecognizer(Recognizer):
    name = "audd"

    def __init__(self, token):
        self.token = token

    def recognize(self, wav_path):
        if not self.token:
            raise RecognizerError("audd_token is empty")
        boundary = "----mvsync" + str(int(time.time() * 1000))
        data = Path(wav_path).read_bytes()
        body = (f"--{boundary}\r\nContent-Disposition: form-data; name=\"api_token\"\r\n\r\n"
                f"{self.token}\r\n--{boundary}\r\nContent-Disposition: form-data; name=\"file\"; "
                f"filename=\"q.wav\"\r\nContent-Type: audio/wav\r\n\r\n").encode() + data + \
               f"\r\n--{boundary}--\r\n".encode()
        req = urllib.request.Request("https://api.audd.io/", data=body, headers={
            "Content-Type": f"multipart/form-data; boundary={boundary}"})
        try:
            out = json.load(urllib.request.urlopen(req, timeout=30))
        except Exception as e:  # noqa
            raise RecognizerError(f"AudD request failed: {type(e).__name__}")  # no token in logs
        if out.get("status") != "success":
            raise RecognizerError(f"AudD error: {out.get('error', {}).get('error_message', '?')}")
        r = out.get("result")
        return {"artist": r["artist"], "title": r["title"]} if r else None


class MockRecognizer(Recognizer):
    """For sim_eval: returns a fixed answer (or None)."""
    name = "mock"

    def __init__(self, answer=None):
        self.answer = answer
        self.calls = 0

    def recognize(self, wav_path):
        self.calls += 1
        return self.answer


def make_recognizer(name, token="", language="ja-JP", country="JP"):
    if name == "audd":
        return AudDRecognizer(token)
    if name == "mock":
        return MockRecognizer()
    return ShazamRecognizer(language, country)


_KATA = re.compile(r"[\u30a0-\u30ff]")
_JA = re.compile(r"[\u3040-\u30ff\u3400-\u9fff]")


def needs_english_artist(artist, title):
    """Katakana artist and a title without Japanese: most likely a Western artist (エミネム - Godzilla)."""
    return bool(_KATA.search(artist or "")) and not _JA.search(title or "")


def match_artist(rec):
    """Artist names to search / match with: the English name first when there is one."""
    en = rec.get("artist_en")
    return f"{en}, {rec['artist']}" if en else rec["artist"]


# ---------------------------------------------------------------- remix -> original MV (todo_20261114 #2)
REMIX_WORDS = ["remix", "bootleg", "flip", "vip", "edit", "rework", "mashup", "re-edit", "refix", "remake"]
# same song, not someone else's version: handled as a normal song (audio-checked)
REMIX_EXCLUDE = ["radio edit", "tv edit", "single edit", "album edit", "short edit", "original mix", "extended mix",
                 "extended", "tv size", "clean", "explicit", "remaster"]


def remix_info(artist, title, words=None, exclude=None):
    """Shazam's title names a remix ("Song (B Remix)", "Song [B Bootleg]", "Song - B VIP")?
    -> {"artist", "title", "remixer", "tag"} of the ORIGINAL song, or None.
    "feat." stays in the title; the remixer is dropped from the artist list ("A & B" for
    "Song (B Remix)" -> "A")."""
    words = [w.lower() for w in (words or REMIX_WORDS)]
    exclude = [w.lower() for w in (exclude or REMIX_EXCLUDE)]
    pat = re.compile(r"(?<![a-z0-9])(" + "|".join(re.escape(w) for w in words) + r")(?![a-z0-9])", re.I)
    t = title or ""
    parts = [(m.start(), m.end(), m.group(1) or m.group(2) or m.group(3) or "")
             for m in re.finditer(r"\(([^()]*)\)|\[([^\[\]]*)\]|（([^（）]*)）", t)]
    m = re.search(r"\s[-–—]\s(.+)$", t)
    if m:
        parts.append((m.start(), len(t), m.group(1)))
    # "RUN REMIX (feat. KREVA & VaVa)": a remix word ending the title outside any bracket
    bare = re.sub(r"\s+", " ", re.sub(r"\([^()]*\)|\[[^\[\]]*\]|（[^（）]*）", " ", t)).strip()
    mb = re.search(r"\s(" + "|".join(re.escape(w) for w in words) + r")$", bare, re.I)
    if mb and not any(x in bare.lower() for x in exclude):
        i = t.lower().rfind(mb.group(1).lower())
        parts.insert(0, (i, i + len(mb.group(1)), mb.group(1)))
    for a, b, g in parts:
        gl = g.lower()
        if not pat.search(gl) or any(x in gl for x in exclude):
            continue
        orig = re.sub(r"\s+", " ", (t[:a] + t[b:])).strip(" -–—")
        if not orig:
            continue
        remixer = re.sub(r"'s$|’s$", "", pat.sub("", g).strip(" -–—'")).strip()
        rn = _norm(remixer)
        arts = [x.strip() for x in re.split(r",|、| & | x | × |/|／", artist or "") if x.strip()]
        keep = [x for x in arts if not (rn and (_norm(x) == rn or _norm(x) in rn or rn in _norm(x)))]
        return {"artist": ", ".join(keep or arts), "title": orig, "remixer": remixer, "tag": g.strip()}
    return None


def names_remix(e, info):
    """The video title names this remix: the remixer (when Shazam gave one) and a remix word
    ("RUN (Kabanagu remix)"), not just the song (which the original MV also does)."""
    t = _norm(e.get("title"))
    words = [w for w in re.findall(r"[a-z][a-z\-]+", info["tag"].lower()) if _norm(w) in t]
    rmx = _norm(info.get("remixer"))
    return bool(words) and (not rmx or rmx in t)


def names_artist(e, artist):
    """The upload credits the artist: own channel, or an artist name in the title (remix path: no
    audio check, so the artist must be named)."""
    if own_channel(e, artist):
        return True
    t = _norm(e.get("title"))
    for a in re.split(r",|、| & | x | × |/|／", re.sub(r"\((?:CV|cv)[^)]*\)", "", artist or "")):
        a = _norm(a)
        if len(a) >= 2 and a in t:
            return True
    return False


def write_wav(path, y, sr=F.SR):
    y = np.clip(np.asarray(y, np.float32), -1, 1)
    with wave.open(str(path), "wb") as w:
        w.setnchannels(1)
        w.setsampwidth(2)
        w.setframerate(sr)
        w.writeframes((y * 32767).astype("<i2").tobytes())


# NG / priority title words: defaults in title_words.py, editable from TD (set_title_words)
BAD_WORDS = TW.NG_WORDS
PRIORITY_RE = TW.priority_regex(TW.PRIORITY_WORDS)


def set_title_words(priority=None, ng=None):
    """Replace the priority / NG words (lists or comma-separated text); None keeps the current."""
    global BAD_WORDS, PRIORITY_RE
    if priority is not None:
        PRIORITY_RE = TW.priority_regex(priority)
    if ng is not None:
        BAD_WORDS = tuple(TW.parse(ng))


# still-image uploads: allowed only when nothing better exists
LABEL_WORDS = (r"records|music|entertainment|label|lantis|flying ?dog|kadokawa|aniplex|avex|sony|"
               r"pony ?canyon|victor|toho|bandai|namco|warner|universal|nbc|king ?amusement|"
               r"anime|アニメ|レコード|ミュージック|エンタテインメント|エンターテインメント|channel|チャンネル")
# kinds of official video accepted besides MVs (11/14 user request): lyric videos (ranked after an MV of
# the same channel group) and anime creditless OP / ED (often titled by the anime, not the song: the
# song name is then looked for in the description)
LYRIC_WORDS = r"lyric|リリック"
ANIME_WORDS = (r"ノンクレジット|non-?credit|creditless|クレジットなし|オープニング|エンディング|"
               r"\bopening\b|\bending\b|\bop\b|\bed\b|\bop\d|\bed\d|op映像|ed映像|opテーマ|edテーマ")
LIVE_WORDS = ("live", "performance", "concert", "tour", "festival", "rehearsal", "ライブ映像")
AUDIO_ONLY = ("official audio", "(audio)", "[audio]", "visualizer", "visualiser", "art track",
              "provided to youtube")


def _norm(s):
    # keep letters/digits of any script (Japanese titles and channel names too);
    # NFKC: full-width "ＯＮＥ" / half-width kana match their usual forms
    return "".join(ch for ch in unicodedata.normalize("NFKC", s or "").lower() if ch.isalnum())


VERSION_WORDS = ("remix", "edit", "ver.", "version", "instrumental", "inst.", "extended", "acoustic",
                 "bootleg", "flip", "vip", "rework", "cover", "tv size", "short")   # not "mix": "Single mix"


def short_artist(artist):
    """Shazam often returns long credits for anime songs, e.g. 'Xs, Yukiho Hagiwara (CV: Azumi
    Asakura), Miki Hoshii (CV: ...) & ...' -> YouTube finds nothing. Keep the first name only."""
    a = re.sub(r"\((?:CV|cv|C\.V\.)[^)]*\)|（CV[^）]*）", "", artist or "")
    a = re.split(r",|、| & | x | × |/|／| feat\.? | ft\.? ", a)[0]
    return re.sub(r"\s+", " ", a).strip()


QUERY_SYMBOLS = "☆★♡♥❤♪♫♬〜～・※◆◇■□●○△▽▲▼†‡∞＊*#"


def search_queries(artist, title):
    """Queries to try in order until one returns usable candidates. Decorative symbols are dropped:
    YouTube returns nothing for "海鮮☆ハイウェイ" but finds "海鮮ハイウェイ"."""
    sa = short_artist(artist)
    t = title
    for ch in QUERY_SYMBOLS:
        sa, t = sa.replace(ch, " "), t.replace(ch, " ")
    sa = re.sub(r"\s+", " ", sa).strip()
    t = re.sub(r"\s+", " ", t).strip()
    qs = [f"{sa} {t} official music video", f"{sa} {t} MV", f"{t} {sa}", t,
          f"{t} ノンクレジット", f"{sa} {t} lyric video"]
    out = []
    for q in qs:
        q = q.strip()
        if q and q not in out:
            out.append(q)
    return out


def names_song(e, title):
    core = _norm(re.split(r" \(| - | \[", title)[0])
    return bool(core) and core in _norm(e.get("title"))


def is_candidate(e, artist, title):
    """Worth downloading the audio to verify: the title names the song. A short title ("ONE")
    also needs the artist in the title or channel (otherwise Metallica "One" etc. qualify)."""
    if not names_song(e, title):
        return False
    core = _norm(re.split(r" \(| - | \[", title)[0])
    if len(core) >= 4:
        return True
    a = _norm(short_artist(artist))
    return bool(a) and a in _norm(" ".join(str(e.get(k) or "") for k in ("title", "channel", "uploader")))


def is_anime_video(e):
    return bool(re.search(ANIME_WORDS, (e.get("title") or "").lower()))


def find_candidates(search, artist, title, official_only=True, bad=(), log=lambda m: None, stale=lambda: False):
    """Search with search_queries() until a query gives a candidate whose title names the song.
    If none does, rank the union of every query's results. Returns (query, entries, ranked, cands):
    cands = titles naming the song, then up to 2 anime creditless OP / ED videos that do not name it
    (marked _need_desc: the song must be in the description, checked with the full metadata)."""
    seen, union, ents, ranked, q = set(), [], [], [], ""
    for q in search_queries(artist, title):
        try:
            ents = search(q, 8)
        except Exception as e:  # noqa
            log(f"yt-dlp search failed: {e}")
            continue
        for e in ents:
            if e.get("id") and e["id"] not in seen:
                seen.add(e["id"])
                union.append(e)
        ranked = [e for e in rank_candidates(ents, artist, title, official_only=official_only)
                  if e.get("id") not in bad]
        if any(is_candidate(e, artist, title) for e in ranked):
            break
        if stale():
            return q, ents, [], []
    else:
        ents = union
        q = "(all queries)"
        ranked = [e for e in rank_candidates(union, artist, title, official_only=official_only)
                  if e.get("id") not in bad]
    named = [e for e in ranked if is_candidate(e, artist, title)]
    anime = [e for e in ranked if e not in named and is_anime_video(e)][:2]
    for e in anime:
        e["_need_desc"] = True
    return q, ents, ranked, named + anime


def is_topic(e):
    """YouTube's auto-generated art tracks ("<artist> - Topic"): a still image + the album audio.
    The channel name does not always say "Topic" (e.g. 21 Savage "redrum"), but the description
    always starts with "Provided to YouTube by"."""
    names = " ".join(str(e.get(k) or "") for k in ("channel", "uploader")).lower()
    desc = (e.get("description") or "").lstrip().lower()
    return names.rstrip().endswith("topic") or " - topic" in names or desc.startswith("provided to youtube")


def official_status(e, artist):
    """True = official upload, False = not, None = cannot tell from this (flat) entry.
    Official: verified channel, "Official / 公式 / VEVO" in the channel name, or a channel that
    is the artist. Topic art tracks are never official MVs here (handled by is_topic)."""
    ch = e.get("channel") or e.get("uploader") or ""
    if own_channel(e, artist):
        return True
    if re.search(r"official|公式|オフィシャル|vevo", ch, re.I):
        return True
    if e.get("channel_is_verified") is True and re.search(LABEL_WORDS, ch, re.I):
        # a verified label / distributor channel (Lantis Channel, KADOKAWAanime, UNIVERSAL MUSIC
        # JAPAN ...). Verified alone is not enough: "カラオケ歌っちゃ王" is verified too
        return True
    if not ch:
        return None
    return False


def own_channel(e, artist):
    """The artist's own channel. Channel names may list several owners ("IA PROJECT and LanPage",
    "higma and 2 more"): one part equal to (or starting with) the artist is enough. A short artist
    name ("IA") must equal a whole word of that part."""
    ch = e.get("channel") or e.get("uploader") or ""
    names = [_norm(a) for a in re.split(r",|、| & | x | × |/|／", re.sub(r"\((?:CV|cv)[^)]*\)", "", artist or ""))]
    names = [a for a in names if a]
    # Japanese YouTube lists owners as "IA PROJECT、LanPage" / "higma、他 2 チャンネル"
    for part in re.split(r"\s+and\s+|\s*&\s*|\s*/\s*|\s*／\s*|\s*,\s*|\s*、\s*", ch):
        pn = _norm(part)
        words = [_norm(w) for w in part.split()]
        for a in names:
            if pn == a or a in words or (len(a) >= 3 and pn.startswith(a) and _generic_suffix(pn[len(a):])):
                return True
        # the title credits this channel as the artist ("【MV】愛のニコニコ / あいの" on channel あいの):
        # Shazam may give the artist in romaji ("Aino"). A third party's re-upload does not carry the
        # re-uploader's own name in the title.
        if pn and pn not in GENERIC_CHANNEL and (len(pn) >= 3 or not pn.isascii()) and pn in _norm(e.get("title")):
            return True
    return False


CHANNEL_SUFFIXES = ("officialchannel", "officialyoutubechannel", "official", "vevo", "music", "channel", "ch",
                    "tv", "records", "staff", "info", "jp", "japan", "mv", "youtube", "公式", "オフィシャル",
                    "チャンネル", "ちゃんねる", "staffch")


def _generic_suffix(rest):
    """What follows the artist name in a channel name is only "Official", "VEVO", "Music", "Ch" ...
    ("EminemMusic", "Twinfield Official"). "エミネムの翻訳動画" (a fan's translation channel) is not."""
    while rest:
        for w in CHANNEL_SUFFIXES:
            if rest.startswith(w):
                rest = rest[len(w):]
                break
        else:
            return False
    return True


GENERIC_CHANNEL = {"music", "official", "mv", "video", "channel", "ch", "tv", "anime", "records", "topic",
                   "musicvideo", "officialchannel", "公式", "チャンネル"}


def rank_candidates(entries, artist, title, official_only=False):
    """Order YouTube search results for the recognised song (spec §4.7-2, plus view counts).
    1. audio-only uploads (Official Audio / Visualizer / Topic) last
    2. titles that are this song first, then this song in another version (an extra word such as
       "Remix" that the recognised title does not have), then titles without the song name
    3. within the same group: more views first (the real MV usually has by far the most)
    4. then "official video / MV" wording, artist channel, a normal song length."""
    a = _norm(artist.split(",")[0].split("&")[0].split(" feat")[0])
    tl = title.lower()
    core = _norm(re.split(r" \(| - | \[", title)[0])
    full = _norm(re.sub(r"\((feat|ft)\.?[^)]*\)", "", title, flags=re.I))   # "(feat. X)" is often not in titles
    out = []
    for i, e in enumerate(entries):
        t = (e.get("title") or "").lower()
        bad = [w for w in BAD_WORDS if w in t and w not in tl]
        if bad and re.search(r"\bmv\b|music video|\bpv\b", t):
            # "echo (Live Performance MV)" is the artist's MV: words about a live show don't count when
            # the title calls itself an MV (the audio check still rejects a real live recording)
            bad = [w for w in bad if w not in LIVE_WORDS]
        if bad:
            continue
        if is_topic(e):
            continue                      # never: auto-generated "Topic" art tracks are not MVs
        if official_only:
            off = official_status(e, artist)
            if off is False:
                continue
            e["_check_official"] = off is None    # re-check with full metadata before verifying
        ch = _norm(e.get("channel") or e.get("uploader"))
        dur = e.get("duration") or 0
        views = int(e.get("view_count") or 0)
        k0 = 1 if any(w in t for w in AUDIO_ONLY) else 0
        extra = [w for w in VERSION_WORDS if w in t and w not in tl]
        # 0 = full recognised title (incl. its version, e.g. "(X Remix)"), 1 = this song,
        # 2 = this song but another version (Remix ...), 3 = title not in it (another song)
        nt = _norm(t)
        if full and full in nt:
            kmatch = 0
        elif core and core in nt:
            kmatch = 2 if extra else 1
        else:
            kmatch = 3
        k2 = 0 if PRIORITY_RE is not None and PRIORITY_RE.search(t) else 1
        klyric = 1 if re.search(LYRIC_WORDS, t) and not re.search(r"music video|\bmv\b|\bpv\b", t) else 0
        k1 = 0 if (a and (ch == a or ch == a + "vevo" or ch.startswith(a))) else 1
        k3 = 0 if 120 <= dur <= 480 else 1
        # the artist's own channel first, then labels etc.; within each, more views first
        kown = 0 if own_channel(e, artist) else 1
        out.append(((k0, kmatch, kown, klyric, -views, k2, k1, k3, i), e))
    out.sort(key=lambda x: x[0])
    return [e for _, e in out]


class OnlineResolver:
    """Runs the online pipeline in a background thread. One job at a time."""

    def __init__(self, worker):
        self.w = worker            # needs: cfg, yt, lib, log, osc_send, get_audio, current_query
        self.busy = False
        self.last_query_t = 0.0
        self.attempts = 0
        self.gen = 0               # bumped when the engine resets: a running lookup is then stale
        self.stage = ""
        self.result = None         # {"artist","title","vid","video_title"}
        self.rejects_path = Path(worker.lib.root) / "rejects.json"
        try:
            self.rejects = json.loads(self.rejects_path.read_text(encoding="utf-8"))
        except Exception:
            self.rejects = {}
        self.stream_urls = {}      # vid -> direct url (until local file ready)
        self.prefetch = {}         # vid -> (thread, holder): direct URL fetched during the audio check
        self.no_mv = set()         # songs whose search named no candidate this session: not searched again
        self.failed = {}           # song -> {vid: failed verifies}; skipped after 2 (one failure may be
                                   # the audio having moved on to the next song during the check)
        self.retry_s = None        # wait before the next lookup, overriding online_retry_s once
        self.must_wait = False     # the last lookup was not counted: still wait before the next
        self.recognizer = make_recognizer(worker.cfg.get("recognizer", "shazamio"),
                                          worker.cfg.get("audd_token", ""),
                                          worker.cfg.get("shazam_language", "ja-JP"),
                                          worker.cfg.get("shazam_country", "JP"))

    def reset_attempts(self):
        self.attempts = 0

    def restart(self):
        """Engine reset (song changed): forget retry limits/waits and cancel a lookup in flight."""
        self.attempts = 0
        self.last_query_t = 0.0
        self.retry_s, self.must_wait = None, False
        if self.busy:
            self.w.log("online: the song changed during identification - lookup cancelled")
        self._release()

    def _stale(self, g):
        if g == self.gen:
            return False
        self._send("cancelled")
        return True

    def _library_match(self, rec, exact=False):
        """A ready library video for the recognised song (by earlier recognition or by title)."""
        def norm(x):
            return "".join(ch for ch in (x or "").lower() if ch.isalnum())
        key = f"{rec['artist']} - {rec['title']}".lower()
        t = norm(rec["title"].split(" (")[0].split(" - ")[0])
        a = norm(rec["artist"].split(",")[0].split("&")[0].split(" feat")[0])
        for vid, m in list(self.w.lib.metas.items()):
            if m.get("status") != "ready":
                continue
            if (m.get("recognised") or "").lower() == key:
                return vid
            if exact:          # a remix: the original song's MV (same title) is not this recording
                continue
            title = norm(m.get("title")) + norm(m.get("channel"))
            if len(t) >= 2 and t in title and (not a or a in title):
                return vid
        return None

    def cancel(self, why):
        """Drop a lookup in flight (e.g. the library locked first)."""
        if self.busy:
            self.w.log(f"online: {why} - lookup cancelled")
            self._release()

    def _release(self):
        """Abandon the lookup in flight right away (it may be stuck in a slow Shazam call):
        bump gen so its result is ignored, and free the slot so a new lookup can start."""
        self.gen += 1
        self.busy = False
        self.w.set_overlay_state(None)

    def can_start(self, now, retry_s=15.0):
        if self.busy:
            return False
        if self.attempts >= 3:
            # 3 tries for one song, then a pause; after it start over (the song may have changed
            # without the library noticing - e.g. nothing was ever LOCKED)
            if now - self.last_query_t < 30.0:
                return False
            self.attempts = 0
        if self.retry_s is not None:
            retry_s = self.retry_s
        if self.attempts == 0 and not self.must_wait:
            return True
        return now - self.last_query_t >= retry_s

    def _not_counted(self, retry_s=None):
        """This lookup told us nothing about the new song (Shazam still hears the previous one, or a
        song already known to have no MV): it does not count towards the 3 tries (whose 30 s pause
        would delay the next song), and the next try may come sooner."""
        self.attempts = max(0, self.attempts - 1)
        self.retry_s = retry_s
        self.must_wait = True

    def start(self, now):
        self.retry_s = None
        self.must_wait = False
        self.busy = True
        self.last_query_t = now
        self.attempts += 1
        threading.Thread(target=self._run, name="online", daemon=True).start()

    def reject(self, vid, key=None):
        if key is None:
            if not self.result:
                return
            key = f"{self.result['artist']} - {self.result['title']}".lower()
        self.rejects.setdefault(key, [])
        if vid not in self.rejects[key]:
            self.rejects[key].append(vid)
        self.rejects_path.write_text(json.dumps(self.rejects, ensure_ascii=False, indent=1),
                                     encoding="utf-8")

    def _send(self, status, video_title=""):
        r = self.result or {}
        self.stage = status
        self.w.osc_send("/mvsync/online", [r.get("artist", ""), r.get("title", ""), video_title, status])

    def _run(self):
        w = self.w
        g = self.gen
        try:
            if not getattr(w, "remix", None):     # periodic re-checks while a remix plays stay quiet
                w.set_overlay_state("IDENTIFYING_ONLINE")
            self.result = None
            self.prefetch = {}     # URLs of earlier lookups' candidates are not needed any more
            y = w.get_audio(12.0)
            if y is None:
                return
            with tempfile.TemporaryDirectory() as td:
                wav = Path(td) / "query.wav"
                write_wav(wav, y)
                try:
                    rec = self.recognizer.recognize(wav)
                except RecognizerTimeout as e:
                    w.log(f"online: {self.recognizer.name} timed out ({e}) - will retry")
                    self._send("timeout")
                    return
                except RecognizerError as e:
                    w.log(f"{self.recognizer.name} が失敗しました。更新してください（Update tools）: {e}")
                    self._send("recognizer_error")
                    return
                if g != self.gen:          # abandoned meanwhile: stay quiet
                    return
                if not rec:
                    w.log(f"online: not recognised (attempt {self.attempts}/3)")
                    self._send("not_recognised")
                    return
                if self._stale(g):
                    return
                self.result = dict(rec)
                w.log(f"online: recognised {rec['artist']} - {rec['title']}"
                      + (f" (English artist name: {rec['artist_en']})" if rec.get("artist_en") else ""))
                key0 = f"{rec['artist']} - {rec['title']}".lower()
                if key0 not in self.no_mv:
                    w.no_mv_song = ""      # another song: no longer "no MV"
                self._send("recognised")
                key = f"{rec['artist']} - {rec['title']}".lower()
                bad = set(self.rejects.get(key, []))
                rm = getattr(w, "remix", None)
                if rm:
                    if rm["key"] == key:
                        # the same remix is still playing: keep the original MV, ask again later
                        self._send("remix_playing", rm.get("title", ""))
                        self._not_counted(float(w.cfg.get("remix_retry_s", 20.0)))
                        return
                    w.stop_remix(f"another song recognised ({rec['artist']} - {rec['title']})")
                info = remix_info(match_artist(rec), rec["title"], w.cfg.get("remix_words"), w.cfg.get("remix_exclude")) \
                    if w.cfg.get("remix_original", True) else None
                if info:
                    # a MV of the remix itself first (audio-checked, so it plays in sync); the original
                    # song's MV only when there is none (11/14: e.g. tofubeats "RUN REMIX")
                    w.log(f"online: {rec['artist']} - {rec['title']} is a remix ({info['tag']}) - "
                          f"looking for a MV of the remix first")
                lv = self._library_match(rec, exact=bool(info))
                if lv and lv not in bad:
                    w.log(f"online: {rec['artist']} - {rec['title']} is in the library ({lv}) "
                          f"- leaving it to library matching")
                    self._send("in_library", (w.lib.meta(lv) or {}).get("title", lv))
                    if lv == getattr(w, "playing_id", None):
                        # the MV still on screen: Shazam heard the end of the previous song (DJ
                        # transition) - ask again soon instead of after online_retry_s
                        self._not_counted(float(w.cfg.get("online_quick_retry_s", 4.0)))
                    return
                if key in self.no_mv and info:
                    self._remix(rec, info, key, bad, g)     # no remix MV (searched before): the original's
                    return
                if key in self.no_mv:
                    w.log(f"online: {rec['artist']} - {rec['title']} has no MV (searched before) "
                          f"- not searching again")
                    w.no_mv_song = f"{rec['artist']} - {rec['title']}"
                    self._send("no_mv")
                    self._not_counted()
                    return
                # already in library? (by id) -> nothing to fetch
                w.set_overlay_state("FETCHING")
                q, ents, ranked, named = find_candidates(
                    w.yt.search, match_artist(rec), rec["title"], bool(w.cfg.get("official_only", True)), bad,
                    w.log, lambda: g != self.gen)
                if self._stale(g):
                    return
                w.log("online: search " + json.dumps({
                    "q": q, "n_results": len(ents),
                    "top5": [{"id": e.get("id"), "title": e.get("title"), "channel": e.get("channel"),
                              "duration": e.get("duration"), "views": e.get("view_count")}
                             for e in ranked[:5]]}, ensure_ascii=False))
                n_anime = sum(1 for e in named if e.get("_need_desc"))
                if len(named) - n_anime < len(ranked):
                    w.log(f"online: {len(ranked) - len(named) + n_anime} result(s) without the song name skipped"
                          + (f" ({n_anime} anime OP/ED kept: song checked in the description)" if n_anime else ""))
                if info:
                    rn = [e for e in named if names_remix(e, info)]
                    if len(rn) < len(named):
                        w.log(f"online: {len(named) - len(rn)} result(s) not naming the remix skipped")
                    named = rn
                failed = self.failed.setdefault(key, {})
                # all named candidates in order; at most 3 audio checks (an unofficial one found by the
                # full-metadata check below no longer uses up a slot, so an official 4th is reached)
                cands = [e for e in named if failed.get(e["id"], 0) < 2]
                if len(cands) < len(named):
                    w.log(f"online: {len(named) - len(cands)} candidate(s) already failed twice - skipped")
                if not cands and info:
                    self.no_mv.add(key)
                    w.log(f"online: no MV of the remix itself")
                    self._remix(rec, info, key, bad, g)
                    return
                if not cands:
                    self.no_mv.add(key)
                    w.log(f"online: no MV for {rec['artist']} - {rec['title']} - will not search it again")
                    w.no_mv_song = f"{rec['artist']} - {rec['title']}"
                    self._send("no_mv")
                    return
                n_checked = 0
                for e in cands:
                    if self._stale(g):
                        return
                    if n_checked >= (2 if info else 3):
                        break
                    vid = e["id"]
                    meta = w.lib.meta(vid)
                    if meta and meta.get("status") == "ready":
                        w.log(f"online: {vid} already in library (not matching?) - skipped")
                        continue
                    if e.get("_check_official") or not e.get("description") or e.get("_need_desc"):
                        # flat search results sometimes lack channel / description (a Topic art
                        # track slipped through that way): look at the full metadata first
                        try:
                            full = w.yt.json(["--no-playlist", "--skip-download",
                                              f"https://www.youtube.com/watch?v={vid}"])
                        except Exception as ex:  # noqa
                            w.log(f"online: metadata failed for {vid}: {ex}")
                            continue
                        if is_topic(full):
                            w.log(f"online: {vid} is a Topic art track - skipped")
                            failed[vid] = 2
                            continue
                        if e.get("_need_desc") and not is_candidate(
                                {"title": full.get("description") or "", "channel": full.get("channel")},
                                match_artist(rec), rec["title"]):
                            w.log(f"online: {vid} '{e.get('title')}' (anime OP/ED) does not name the song "
                                  f"in its description - skipped")
                            failed[vid] = 2
                            continue
                        if w.cfg.get("official_only", True) and not official_status(full, match_artist(rec)):
                            w.log(f"online: {vid} '{e.get('title')}' is not an official upload "
                                  f"({full.get('channel')}) - skipped")
                            failed[vid] = 2
                            continue
                    self._send("verifying", e.get("title", ""))
                    n_checked += 1
                    if w.cfg.get("online_prefetch_url", True):
                        self._prefetch_url(vid)
                    try:
                        w.yt.run(["-f", "ba", "--no-playlist", "--no-part", "-o",
                                  str(Path(td) / f"{vid}.%(ext)s"),
                                  f"https://www.youtube.com/watch?v={vid}"], timeout=300)
                        f = next(Path(td).glob(f"{vid}.*"))
                        onset = F.features_full(F.decode_audio(f, w.ffmpeg))
                    except Exception as ex:  # noqa
                        w.log(f"online: audio fetch failed for {vid}: {ex}")
                        continue
                    qf, qb, t0 = w.current_query()
                    if qf is None:
                        continue
                    bank = RefBank()
                    bank.add(vid, onset)
                    rates = np.round(np.arange(w.cfg["rate_range"][0], w.cfg["rate_range"][1] + 1e-9,
                                               0.01), 4)
                    best = bank.global_search(qf, qb, t0, rates, [vid], top_k=1)
                    score = best[0].score if best else 0.0
                    w.log(f"online: verify {vid} '{e.get('title')}' views={e.get('view_count') or '?'} "
                          f"rank={cands.index(e) + 1} dur={e.get('duration')} score={score:.3f}")
                    if score < float(w.cfg.get("theta_verify", 0.7)):
                        if not self._stale(g):
                            failed[vid] = failed.get(vid, 0) + 1
                        continue
                    if self._stale(g):
                        return
                    self._adopt(vid, e, onset, rec, g, best[0])
                    return
                if info and not self._stale(g):
                    w.log("online: no MV of the remix itself passed the audio check")
                    self._remix(rec, info, key, bad, g)
                    return
                self._send("no_match")
        except Exception as e:  # noqa
            w.log(f"online pipeline error: {type(e).__name__}: {e}")
        finally:
            if g == self.gen:            # an abandoned lookup must not clear a newer one
                self.busy = False
                w.set_overlay_state(None)

    def _remix(self, rec, info, key, bad, g):
        """Shazam named a remix: play the ORIGINAL song's official MV from the top (no position
        sync, no audio check - a remix does not match the original's audio). The upload must also
        name the artist (own channel or the artist in the title)."""
        w = self.w
        orec = {"artist": info["artist"], "title": info["title"]}
        w.log(f"online: {rec['artist']} - {rec['title']} is a remix ({info['tag']}) - "
              f"looking for the original MV: {orec['artist']} - {orec['title']}")
        self._send("remix")
        lv = self._library_match(orec)
        if lv and lv not in bad:
            m = w.lib.meta(lv) or {}
            w.log(f"online: original MV in the library ({lv})")
            self._start_remix(lv, m.get("title", lv), m.get("duration"), key, g)
            return
        okey = key + " | original"
        if okey in self.no_mv:
            w.log(f"online: no original MV for {rec['artist']} - {rec['title']} (searched before)")
            w.no_mv_song = f"{rec['artist']} - {rec['title']}"
            self._send("no_mv")
            self._not_counted()
            return
        w.set_overlay_state("FETCHING")
        q, ents, ranked, cands = find_candidates(w.yt.search, orec["artist"], orec["title"],
                                                 bool(w.cfg.get("official_only", True)), bad, w.log,
                                                 lambda: g != self.gen)
        if self._stale(g):
            return
        w.log("online: remix search " + json.dumps({
            "q": q, "top5": [{"id": e.get("id"), "title": e.get("title"), "channel": e.get("channel")}
                             for e in ranked[:5]]}, ensure_ascii=False))
        # played from the top and looped: a full-length MV before an anime TV-size OP / ED (~90 s)
        cands = sorted(cands, key=is_anime_video)
        for e in cands[:5]:
            if self._stale(g):
                return
            vid = e["id"]
            try:
                full = w.yt.json(["--no-playlist", "--skip-download", f"https://www.youtube.com/watch?v={vid}"])
            except Exception as ex:  # noqa
                w.log(f"online: metadata failed for {vid}: {ex}")
                continue
            why = ""
            if is_topic(full):
                why = "Topic art track"
            elif w.cfg.get("official_only", True) and not official_status(full, orec["artist"]):
                why = f"not an official upload ({full.get('channel')})"
            elif e.get("_need_desc") and not is_candidate(
                    {"title": full.get("description") or "", "channel": full.get("channel")},
                    orec["artist"], orec["title"]):
                why = "anime OP/ED without the song in its description"
            elif not names_artist(full, orec["artist"]) and not (
                    e.get("_need_desc") and _norm(short_artist(orec["artist"])) in _norm(full.get("description"))):
                why = "does not name the artist"
            if why:
                w.log(f"online: remix: {vid} '{e.get('title')}' skipped - {why}")
                continue
            self._start_remix(vid, e.get("title", ""), full.get("duration") or e.get("duration"), key, g, e)
            return
        self.no_mv.add(okey)
        w.log(f"online: no original MV for {rec['artist']} - {rec['title']} - will not search it again")
        w.no_mv_song = f"{rec['artist']} - {rec['title']}"
        self._send("no_mv")

    def _start_remix(self, vid, title, duration, key, g, e=None):
        w = self.w
        if not (w.lib.meta(vid) or {}).get("status") == "ready":
            try:
                url = self._direct_url(vid)
            except Exception as ex:  # noqa
                w.log(f"online: direct URL failed for {vid}: {ex}")
                return
            if self._stale(g):
                return
            self.stream_urls[vid] = url + "&tdext=.mp4"
            if w.lib.meta(vid) is None:
                (Path(w.lib.libdir) / vid).mkdir(parents=True, exist_ok=True)
                w.lib._write_meta(vid, title=title, channel=(e or {}).get("channel"), duration=duration,
                                  source="auto")
                w.lib.enqueue(vid, source="auto", priority=True)   # cache: the original may be played later
            else:
                w.lib.enqueue(vid, source=None, priority=True)
        if not w.start_remix(vid, key, float(duration or 0), title):
            w.log(f"online: original MV {vid} not shown (library locked / manual select)")
            return
        self.result["vid"] = vid
        self.result["video_title"] = title
        self._send("remix_adopted", title)
        w.log(f"online: remix -> original MV {vid} '{title}' from the top")
        # keep asking Shazam every remix_retry_s: the only way to notice the next song (no position sync)
        self.attempts = 0
        self.retry_s = float(w.cfg.get("remix_retry_s", 20.0))
        self.must_wait = True

    def _prefetch_url(self, vid):
        """Fetch the direct URL while the audio is downloaded for the check (~6 s saved on adopt).
        Wasted for a candidate that fails the check."""
        if vid in self.prefetch:
            return
        holder = {"t0": time.time()}

        def job():
            try:
                holder["url"] = self.w.yt.direct_url(f"https://www.youtube.com/watch?v={vid}", "18")
            except Exception as ex:  # noqa
                holder["err"] = ex
            holder["t1"] = time.time()
        th = threading.Thread(target=job, name=f"url-{vid}", daemon=True)
        self.prefetch[vid] = (th, holder)
        th.start()

    def _direct_url(self, vid):
        w = self.w
        pf = self.prefetch.pop(vid, None)
        if pf is not None:
            th, holder = pf
            t = time.time()
            th.join(60)
            w.log(f"online: direct URL prefetched for {vid} (fetch {holder.get('t1', time.time()) - holder['t0']:.1f} s, "
                  f"waited {time.time() - t:.1f} s after the check)")
            if holder.get("url"):
                return holder["url"]
            if "err" in holder:
                w.log(f"online: prefetched direct URL failed for {vid}: {holder['err']} - retrying")
        # takes 2-6 s (YouTube needs a moment before the URL works)
        return w.yt.direct_url(f"https://www.youtube.com/watch?v={vid}", "18")

    def _adopt(self, vid, e, onset, rec, g=None, cand=None):
        w = self.w
        try:
            url = self._direct_url(vid)
        except Exception as ex:  # noqa
            url = None
            w.log(f"online: direct URL failed for {vid}: {ex}")
        if g is not None and self._stale(g):      # cancelled meanwhile (song changed / library locked)
            w.log(f"online: {vid} not adopted - lookup was cancelled")
            return
        if url:
            self.stream_urls[vid] = url + "&tdext=.mp4"
        d = Path(w.lib.libdir) / vid
        d.mkdir(parents=True, exist_ok=True)
        F.save_ref(d / "ref.npz", onset)
        # already listed in urls.txt / a playlist but not downloaded yet: just move it to the front
        known = w.lib.meta(vid) is not None
        if known:
            w.lib._write_meta(vid, recognised=f"{rec['artist']} - {rec['title']}")
        else:
            w.lib._write_meta(vid, title=e.get("title"), channel=e.get("channel"),
                              duration=e.get("duration"), source="auto",
                              recognised=f"{rec['artist']} - {rec['title']}")
        w.lib.enqueue(vid, source=None if known else "auto", priority=True)
        # not written to urls.txt any more: online finds are cache (deleted when TD exits)
        w.add_ref(vid, onset)
        if cand is not None and w.cfg.get("online_provisional", True):
            w.show_provisional(cand)
        self.result["vid"] = vid
        self.result["video_title"] = e.get("title", "")
        self._send("adopted", e.get("title", ""))
        w.log(f"online: adopted {vid} '{e.get('title')}'")
        # done for this song: no further lookups until the library locks (which resets this)
        self.attempts = 3

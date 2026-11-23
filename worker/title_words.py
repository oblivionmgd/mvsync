"""Title words for the online MV search, editable from TD (Config page: Priority words / NG words).
2026-11-15. No dependencies: also read by td/build_mvsync.py for the parameter defaults.

Both lists are comma-separated in TD and lists in config.json ("title_priority_words",
"title_ng_words"). Matching is case-insensitive on the YouTube title:
- NG word: anywhere in the title (substring), unless the recognised song title has it too
  ("Remix" is NG, but not when Shazam names a remix). Live-show words are ignored when the title
  calls itself an MV ("echo (Live Performance MV)").
- Priority word: a video whose title has one ranks before one without, after the other rules
  (song name in the title, own channel, views). A word ending in a Latin letter or digit must not
  be followed by another letter (any script) nor preceded by a Latin letter / digit: "op" matches
  "OP", "OP2", "チェンソーマンOP" but not "opera" or "OP映像" (listed on its own).
"""
import re

PRIORITY_WORDS = (
    # MV wording
    "official music video", "official video", "music video", "mv", "pv", "official", "公式",
    # anime creditless OP / ED
    "ノンクレジット", "non-credit", "noncredit", "creditless", "クレジットなし", "オープニング", "エンディング",
    "opening", "ending", "op", "ed", "op映像", "ed映像", "opテーマ", "edテーマ",
)

NG_WORDS = (
    "live", "reaction", "cover", "sped up", "speed up", "nightcore", "slowed",
    "karaoke", "instrumental", "8d", "remix", "rehearsal", "concert", "performance",
    "tour", "festival", "tiktok", "dance practice", "fan made", "fanmade", "mashup",
    "teaser", "trailer", "behind the", "behind-the", "vlog", "making of", "bts video",
    "interview", "mmd", "amv", "歌ってみた", "踊ってみた", "弾いてみた", "叩いてみた",
    "fan animation", "self-made", "カラオケ", "切り抜き", "ライブ映像",
    "和訳", "翻訳", "日本語訳", "日本語字幕", "歌詞訳", "sub jp", "sub. jp", "subjp", "jp sub",
    "中文字幕", "中字", "繁中", "中翻", "eng sub", "가사", "번역",
    "8bit", "8-bit", "chiptune", "ファミコン風", "風アレンジ", "オルゴール", "music box", "piano ver",
)


def to_text(words):
    return ", ".join(words)


def parse(value):
    """'a, b、c' or a list -> ['a', 'b', 'c'] (lower case, blanks dropped, order kept)."""
    if isinstance(value, str):
        items = re.split(r"[,、，\n]", value)
    else:
        items = list(value or [])
    out = []
    for w in items:
        w = re.sub(r"\s+", " ", str(w)).strip().lower()
        if w and w not in out:
            out.append(w)
    return out


def priority_regex(words):
    """One regex for the priority words (None if the list is empty)."""
    parts = []
    for w in parse(words):
        p = re.escape(w)
        if re.match(r"[a-z0-9]", w):
            p = r"(?<![a-z0-9])" + p
        if re.search(r"[a-z0-9]$", w):
            p += r"(?![^\W\d_])"         # no letter of any script after: "Official髭男dism" is not "official"
        parts.append(p)
    return re.compile("|".join(parts)) if parts else None

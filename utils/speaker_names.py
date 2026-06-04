"""Validate and extract real person names (filter Google Meet UI labels). No regex."""

_INVALID_EXACT = frozenset(
    {
        "dial in",
        "dial-in",
        "call in",
        "call-in",
        "unknown",
        "reframe",
        "fantasy",
        "geez",
        "thanks",
        "thank",
        "thank you",
        "hand raises",
        "hand raise",
        "raise hand",
        "lower hand",
        "backgrounds and effects",
        "backgrounds & effects",
        "english",
        "dutch",
        "french",
        "german",
        "hindi",
        "italian",
        "japanese",
        "korean",
        "russian",
        "swahili",
        "close",
        "copy",
        "host",
        "chat",
        "caption",
        "captions",
        "microphone",
        "camera",
        "share",
        "present",
        "leave",
        "join",
        "meet",
        "google",
        "more",
        "add",
        "dial",
        "other",
        "others",
        "your",
    }
)

_INVALID_SUBSTRINGS = (
    "feature",
    "notification",
    "panel",
    "control",
    "settings",
    "option",
    "caption",
    "language",
    "font",
    "meeting",
    "screen",
    "video",
    "audio",
    "microphone",
    "camera",
    "reaction",
    "activit",
    "turn on",
    "turn off",
    "leave call",
    "copy link",
    "host control",
    "side panel",
    "getting items",
    "call ending",
    "phone number",
    "dial in",
    "dial-in",
    "call in",
    "call-in",
    "beta",
)

_UI_SUFFIXES = (
    "devices",
    "device",
    "microphone",
    "camera",
    "muted",
    "speaking",
    "presenting",
    "host",
)

_BAD_STARTS = (
    "checking",
    "looking",
    "going",
    "thank",
    "hello",
    "yeah",
    "ok",
    "okay",
    "well",
    "right",
    "all",
    "no",
    "yes",
    "gee",
    "geez",
)

_BAD_WORDS = ("that one", "background", "effect", "raise", "hand", "pin", "unpin")

_UI_VERBS = ("for", "with", "in", "the", "this", "your", "turn", "open", "send", "getting")

_BAD_CHARS = "<>{}[]|\\/`~"


def _strip_ui_suffix(s: str) -> str:
    lower = s.lower()
    for suffix in _UI_SUFFIXES:
        if lower.endswith(suffix):
            s = s[: -len(suffix)].strip()
            lower = s.lower()
    return s


def _leading_title_words(s: str, max_words: int = 3) -> str:
    words = []
    for w in s.split():
        if not w or not w[0].isupper():
            break
        tail = w[1:].replace("-", "").replace("'", "")
        if not tail.isalpha():
            break
        words.append(w)
        if len(words) >= max_words:
            break
    return " ".join(words)


def _split_glued_name(s: str) -> str:
    """Jane SmithJane Smith -> Jane Smith; Jane SmithJohn -> Jane Smith."""
    for i in range(1, len(s)):
        if s[i].isupper() and s[i - 1].islower():
            return s[:i].strip()
    return s


def canonicalize_speaker_name(name: str) -> str | None:
    s = (name or "").strip()
    if not s:
        return None
    s = _strip_ui_suffix(s)
    if not s:
        return None

    words = s.split()
    if len(words) >= 4 and len(words) % 2 == 0:
        half = len(words) // 2
        if words[:half] == words[half:]:
            s = " ".join(words[:half])

    if any(ch.isupper() for ch in s[1:]):
        glued = _split_glued_name(s)
        if glued != s:
            s = glued

    leading = _leading_title_words(s)
    if leading:
        s = leading

    return s if is_valid_person_name(s) else None


def _looks_like_name_word(word: str) -> bool:
    if not word or not word[0].isupper():
        return False
    for ch in word[1:]:
        if not (ch.isalpha() or ch in "-'"):
            return False
    return True


def is_valid_person_name(name: str) -> bool:
    s = (name or "").strip()
    if len(s) < 2 or len(s) > 40:
        return False

    lower = s.lower()
    if lower in _INVALID_EXACT:
        return False
    if any(sub in lower for sub in _INVALID_SUBSTRINGS):
        return False
    if "@" in s or "http" in lower:
        return False
    if any(c in s for c in _BAD_CHARS):
        return False
    if len(s.split()) > 4:
        return False
    if not any(c.isalpha() for c in s):
        return False
    if any(c in s for c in ".!?,:;"):
        return False
    if any(lower.startswith(p) for p in _BAD_STARTS):
        return False
    if any(w in lower for w in _BAD_WORDS):
        return False

    words = s.split()
    if len(words) < 2:
        return False
    if not all(_looks_like_name_word(w) for w in words):
        return False
    if len(words) >= 3 and any(v in lower for v in _UI_VERBS):
        return False
    return True


def extract_person_from_ui_label(label: str) -> str | None:
    key = "more options for "
    idx = (label or "").lower().find(key)
    if idx < 0:
        return None
    candidate = label[idx + len(key) :].split(".")[0].strip()
    return candidate if is_valid_person_name(candidate) else None


def filter_person_names(names: list) -> list[str]:
    out: list[str] = []
    for raw in names:
        s = str(raw).strip()
        if not s:
            continue
        extracted = extract_person_from_ui_label(s)
        if extracted:
            s = extracted
        else:
            canon = canonicalize_speaker_name(s)
            if canon:
                s = canon
        if is_valid_person_name(s) and s not in out:
            out.append(s)
    return out[:15]


def compress_speaker_timeline(
    events: list[dict],
    duration_sec: float,
    *,
    max_events: int = 30,
) -> list[dict]:
    if not events:
        return []

    cleaned: list[dict] = []
    for e in events:
        if not isinstance(e, dict):
            continue
        raw = str(e.get("name", "")).strip()
        if not raw:
            continue
        name = canonicalize_speaker_name(raw) or raw
        if not is_valid_person_name(name):
            continue
        try:
            t = int(e.get("t", 0))
        except (TypeError, ValueError):
            t = 0
        if t < 0:
            t = 0
        if cleaned and cleaned[-1]["name"] == name:
            continue
        cleaned.append(
            {
                "t": t,
                "name": name,
                "source": str(e.get("source", "")).strip() or "unknown",
            }
        )

    if not cleaned:
        return []

    if len(cleaned) > max_events:
        step = max(1, len(cleaned) // max_events)
        sampled = [cleaned[i] for i in range(0, len(cleaned), step)]
        if sampled[-1] != cleaned[-1]:
            sampled.append(cleaned[-1])
        cleaned = sampled

    duration_ms = max(int(duration_sec * 1000), 1000)
    max_t = cleaned[-1]["t"]
    if max_t > duration_ms * 1.15 or max_t > duration_ms + 5000:
        scale = duration_ms / max_t if max_t > 0 else 1.0
        for e in cleaned:
            e["t"] = int(e["t"] * scale)

    if cleaned[0]["t"] != 0:
        cleaned[0]["t"] = 0

    return cleaned


_CAPTION_SOURCES = frozenset(
    {"transcript_block", "caption", "captions_final", "transcript_live"}
)
_ACTIVE_SPEAKER_SOURCES = frozenset(
    {"active_speaker", "active_speaker_final", *_CAPTION_SOURCES}
)


def build_speaker_timeline(
    raw_events: list[dict],
    roster: list[str],
    duration_sec: float,
    recorder_name: str | None = None,
) -> list[dict]:
    """
    Build a timeline from real Meet events only (no fake 50/50 roster split).
    Captions/transcript blocks are more reliable than active-speaker UI alone.
    """
    roster_set = set(filter_person_names(roster))
    if not roster_set:
        return []

    normalized = sanitize_speaker_events(raw_events)
    normalized = [e for e in normalized if e.get("name") in roster_set]
    if not normalized:
        return []

    caption_ev = [e for e in normalized if e.get("source") in _CAPTION_SOURCES]
    if caption_ev:
        pool = caption_ev
    else:
        active = [e for e in normalized if e.get("source") in _ACTIVE_SPEAKER_SOURCES]
        pool = active if len({e["name"] for e in active}) >= 2 else normalized

    if len(pool) > 1:
        pool = [e for e in pool if e.get("source") != "recording_start"]

    transitions: list[dict] = []
    prev = None
    for e in sorted(pool, key=lambda x: int(x.get("t", 0))):
        if e["name"] == prev:
            continue
        transitions.append(e)
        prev = e["name"]

    timeline = compress_speaker_timeline(transitions or pool, duration_sec)
    distinct = {e["name"] for e in timeline}

    if len(distinct) >= 2:
        return timeline

    recorder = (recorder_name or "").strip()

    if len(distinct) == 1:
        only = timeline[0]["name"]
        if (
            recorder
            and recorder in roster_set
            and only != recorder
            and not other_speaker_proven_in_captions(caption_ev, recorder, roster_set)
        ):
            return [{"t": 0, "name": recorder, "source": "recorder_primary"}]
        return [{"t": 0, "name": only, "source": "single_detected_speaker"}]

    if recorder and recorder in roster_set:
        return [{"t": 0, "name": recorder, "source": "recorder_default"}]

    return timeline


_MONOLOGUE_MARKERS = (
    "from my side",
    "for my side",
    "that's it for my side",
    "on my side",
    "my update",
    "i'm continuing",
    "i am continuing",
)


def transcript_is_recorder_monologue(transcript: str) -> bool:
    """Spoken update by the person giving their own status (mic user)."""
    lower = (transcript or "").lower()
    return any(m in lower for m in _MONOLOGUE_MARKERS)


def other_speaker_proven_in_captions(
    caption_events: list[dict],
    recorder: str,
    roster_set: set[str],
) -> bool:
    """True when captions show a non-recorder speaking over a real time span."""
    other_times = [
        int(e.get("t", 0))
        for e in caption_events
        if e.get("name") in roster_set and e.get("name") != recorder
    ]
    if len(other_times) >= 2:
        return max(other_times) - min(other_times) > 2500
    return False


def events_for_segment_mapping(events: list[dict]) -> list[dict]:
    """Prefer caption/transcript speaker events over active-speaker UI noise."""
    caption = [e for e in events if e.get("source") in _CAPTION_SOURCES]
    if caption:
        return sorted(caption, key=lambda x: int(x.get("t", 0)))
    return sorted(events, key=lambda x: int(x.get("t", 0)))


def sanitize_speaker_events(events: list[dict]) -> list[dict]:
    clean = []
    for e in events:
        if not isinstance(e, dict):
            continue
        raw = str(e.get("name", "")).strip()
        name = canonicalize_speaker_name(raw) or raw
        if not is_valid_person_name(name):
            continue
        item = dict(e)
        item["name"] = name
        clean.append(item)
    return clean

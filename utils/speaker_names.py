"""Validate and extract real person names (filter Google Meet UI labels)."""
import re

# Meet / phone / UI labels that are NOT people
_INVALID_NAME_RE = re.compile(
    r"^(?:dial[\s-]?in|call[\s-]?in|unknown|reframe|fantasy|geez|thanks?|thank you|"
    r"hand raises?|raise hand|lower hand|backgrounds and effects|backgrounds & effects|"
    r"english|dutch|french|german|hindi|italian|japanese|korean|russian|swahili|"
    r"close|copy|host|chat|captions?|microphone|camera|share|present|leave|join|meet|google|"
    r"more|add|dial|others?|your)$",
    re.I,
)

_INVALID_SUBSTR_RE = re.compile(
    r"\b(?:feature|notification|panel|controls?|settings?|options?|caption|language|"
    r"font|meeting|screen|video|audio|microphone|camera|reaction|activities?|"
    r"turn on|turn off|leave call|copy link|host control|side panel|"
    r"getting items|call ending|phone numbers?|dial[\s-]?in|call[\s-]?in|beta)\b",
    re.I,
)

_EXTRACT_NAME_RE = re.compile(
    r"more options for\s+(.+?)(?:\.|$)",
    re.I,
)


_UI_SUFFIX_RE = re.compile(
    r"\s*(?:devices?|microphone|camera|muted|speaking|presenting|host)\s*$",
    re.I,
)

# Meet DOM sometimes glues labels: "Keshavi DubeyKeshavi Dubeydevices"
_LEADING_NAME_RE = re.compile(r"^([A-Z][a-z]+(?:\s+[A-Z][a-z]+){0,2})")


def canonicalize_speaker_name(name: str) -> str | None:
    """Extract a single real person name from a noisy Meet UI string."""
    s = (name or "").strip()
    if not s:
        return None
    s = _UI_SUFFIX_RE.sub("", s).strip()
    if not s:
        return None

    words = s.split()
    if len(words) >= 4 and len(words) % 2 == 0:
        half = len(words) // 2
        if words[:half] == words[half:]:
            s = " ".join(words[:half])

    glued = re.match(r"^([A-Z][a-z]+(?:\s+[A-Z][a-z]+){0,2})(?=[A-Z][a-z])", s)
    if glued:
        s = glued.group(1).strip()

    m = _LEADING_NAME_RE.match(s)
    if m:
        s = m.group(1).strip()

    return s if is_valid_person_name(s) else None


def is_valid_person_name(name: str) -> bool:
    s = (name or "").strip()
    if len(s) < 2 or len(s) > 40:
        return False
    if _INVALID_NAME_RE.match(s):
        return False
    if _INVALID_SUBSTR_RE.search(s):
        return False
    if "@" in s or "http" in s.lower():
        return False
    if re.search(r"[<>{}[\]|\\/`~]", s):
        return False
    if len(s.split()) > 4:
        return False
    if not re.search(r"[a-zA-Z]", s):
        return False
    if re.search(r"[.!?,:;]", s):
        return False
    if re.search(
        r"^(?:checking|looking|going|thank|hello|yeah|so|ok|okay|well|right|all|no|yes|gee|geez)\b",
        s,
        re.I,
    ):
        return False
    if re.search(r"\b(?:that one|background|effect|raises?|hand|pin|unpin)\b", s, re.I):
        return False
    words = s.split()
    # Real attendees are First Last; single words are Meet UI (More, Others, Add).
    if len(words) < 2:
        return False
    if not all(re.match(r"^[A-Z][a-zA-Z'-]+$", w) for w in words):
        return False
    # UI phrases (multiple words, contains action verbs)
    if re.search(r"\b(?:for|with|in|the|this|your|turn|open|send|getting)\b", s, re.I) and len(
        s.split()
    ) >= 3:
        return False
    return True


def extract_person_from_ui_label(label: str) -> str | None:
    m = _EXTRACT_NAME_RE.search(label or "")
    if m:
        candidate = m.group(1).strip()
        if is_valid_person_name(candidate):
            return candidate
    return None


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
    """
    Collapse duplicate consecutive speakers and scale timestamps to audio length.
    Fixes extension sending 50+ stale caption blocks (e.g. t up to 560s for a 67s file).
    """
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


_ACTIVE_SPEAKER_SOURCES = frozenset(
    {"active_speaker", "active_speaker_final", "transcript_live", "caption", "captions_final"}
)


def build_speaker_timeline(
    raw_events: list[dict],
    roster: list[str],
    duration_sec: float,
) -> list[dict]:
    """
    Prefer live/active speaker events; roster names only; scale to audio length.
    """
    roster_set = set(filter_person_names(roster))
    if not roster_set:
        return []

    normalized = sanitize_speaker_events(raw_events)
    normalized = [e for e in normalized if e.get("name") in roster_set]
    if not normalized:
        return []

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

    if len({e["name"] for e in transitions}) < 2 and len(roster_set) >= 2:
        transitions = []
        prev = None
        for e in sorted(normalized, key=lambda x: int(x.get("t", 0))):
            if e["name"] == prev:
                continue
            transitions.append(e)
            prev = e["name"]

    timeline = compress_speaker_timeline(transitions or pool, duration_sec)

    if len(roster_set) >= 2 and len({e["name"] for e in timeline}) < 2:
        names = [n for n in filter_person_names(roster) if n in roster_set]
        duration_ms = max(int(duration_sec * 1000), 1000)
        if len(names) == 2:
            timeline = [
                {"t": 0, "name": names[0], "source": "roster_alternate"},
                {"t": duration_ms // 2, "name": names[1], "source": "roster_alternate"},
            ]
        elif len(names) >= 3:
            step = duration_ms // len(names)
            timeline = [
                {"t": i * step, "name": names[i], "source": "roster_alternate_3p"}
                for i in range(len(names))
            ]

    return timeline


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

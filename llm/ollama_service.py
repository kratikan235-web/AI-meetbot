import re

from utils.speaker_names import filter_person_names, is_valid_person_name


def generate_mom(transcript: str) -> str:
    """Build MOM from transcript only (no LLM — reliable with small models)."""
    sentences = _split_sentences(transcript)
    if not sentences:
        sentences = [transcript.strip()]

    sentences = [s for s in sentences if not _is_noise(s)]
    if not sentences:
        sentences = [transcript.strip()]

    topic = sentences[0]
    if len(topic) > 120:
        topic = topic[:117] + "..."

    discussion = "\n".join(f"- {s}" for s in sentences)
    lower = transcript.lower()

    progress = _bullets_matching(
        sentences,
        r"done|finished|completed|built|fixed|working|converted|extension|api|record|learning|used|walk",
    )
    blockers = _bullets_matching(sentences, r"block|issue|problem|stuck|error")
    actions = _bullets_matching(
        sentences,
        r"need to|should|will|next|create|generate|fix|try|start",
    )

    return f"""## Topic
{topic}

## Discussion
{discussion}

## Progress
{progress or "- Not mentioned"}

## Blockers
{blockers or "- None mentioned"}

## Action Items
{actions or "- Not mentioned"}

## Next Steps
{actions or "- Not mentioned"}
"""


def _build_discussion_summary(
    speaker_segments: list[dict],
    participants: list[str],
) -> list[str]:
    """Summarize themes — do not paste raw transcript lines."""
    combined = " ".join(
        str(s.get("text", "")) for s in speaker_segments if str(s.get("text", "")).strip()
    ).lower()
    if not combined.strip():
        return ["(Not captured)"]

    bullets: list[str] = []
    if len(participants) >= 2:
        bullets.append("Introduction and conversation between participants.")

    if re.search(r"\b(work|working|project|task|implement|build|code|extension)\b", combined):
        bullets.append("Discussion about current work and progress.")

    if re.search(r"\b(test|testing|check|verify|detect|functionality|assistant|bot)\b", combined):
        bullets.append("Testing of meeting assistant and speaker detection.")

    if re.search(r"\b(plan|next|will|should|need to|action)\b", combined):
        bullets.append("Planning and follow-up items were mentioned.")

    if re.search(r"\b(money|home|fine|hello|hi|how are)\b", combined) and len(bullets) <= 1:
        bullets.append("Informal check-in between participants.")

    if not bullets:
        bullets.append("General discussion between meeting participants.")

    return bullets[:5]


def _build_action_items(speaker_segments: list[dict], participants: list[str]) -> list[str]:
    items: list[str] = []
    for seg in speaker_segments:
        speaker = str(seg.get("speaker") or "").strip()
        if not is_valid_person_name(speaker):
            continue
        text = str(seg.get("text", "")).strip()
        if re.search(
            r"\b(i will|i'll|we will|we'll|need to|should|have to|must|going to)\b",
            text,
            re.I,
        ):
            short = text.rstrip(".")
            if len(short) > 90:
                short = short[:87] + "..."
            items.append(f"* {speaker} → {short}.")
    return items[:6]


def _build_decisions(speaker_segments: list[dict]) -> list[str]:
    items: list[str] = []
    for seg in speaker_segments:
        text = str(seg.get("text", "")).strip()
        if re.search(r"\b(decided|agreed|confirmed|let's|we will continue|go ahead)\b", text, re.I):
            items.append(f"* {text.rstrip('.')}.")
    if not items and re.search(r"\b(test|testing|continue)\b", " ".join(
        str(s.get("text", "")) for s in speaker_segments
    ).lower()):
        items.append("* Continue testing speaker detection functionality.")
    return items[:5]


def generate_speaker_aware_mom(
    speaker_segments: list[dict],
    participants: list[str] | None = None,
) -> str:
    """
    Produce a MOM in the user's requested format:

    Meeting Summary
    Attendees
    Discussion
    Action Items
    Decisions
    """
    participants = filter_person_names(participants or [])
    if not participants:
        seen: list[str] = []
        for s in speaker_segments:
            name = str(s.get("speaker") or "").strip()
            if is_valid_person_name(name) and name not in seen:
                seen.append(name)
        participants = seen

    discussion_items = _build_discussion_summary(speaker_segments, participants)
    action_items = _build_action_items(speaker_segments, participants)
    decisions = _build_decisions(speaker_segments)

    attendees_md = "\n".join(f"* {p}" for p in participants) if participants else "* (Unknown)"
    discussion_md = "\n".join(f"* {d}" for d in discussion_items)
    actions_md = "\n".join(action_items) if action_items else "* None identified."
    decisions_md = "\n".join(decisions) if decisions else "* None identified."

    return f"""Meeting Summary

Attendees:

{attendees_md}

Discussion:

{discussion_md}

Action Items:

{actions_md}

Decisions:

{decisions_md}
"""


def _split_sentences(text: str) -> list[str]:
    parts = [s.strip() for s in re.split(r"(?<=[.!?])\s+", text) if s.strip()]
    return parts


def _is_noise(sentence: str) -> bool:
    lower = sentence.lower()
    noise = (
        "subscribe to my channel",
        "press the bell icon",
        "don't miss any of my videos",
    )
    return any(n in lower for n in noise)


def _bullets_matching(sentences: list[str], pattern: str) -> str:
    matched = [f"- {s}" for s in sentences if re.search(pattern, s, re.I)]
    return "\n".join(matched)

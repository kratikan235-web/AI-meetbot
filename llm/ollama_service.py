import re


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

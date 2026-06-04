"""Small string helpers (no regex)."""


def contains_any(text: str, phrases: tuple[str, ...]) -> bool:
    lower = text.lower()
    return any(p in lower for p in phrases)


def word_in_text(word: str, text: str) -> bool:
    """True if word appears as its own token."""
    w = word.lower()
    for token in text.lower().replace(",", " ").replace(".", " ").split():
        if token.strip("':;!?") == w:
            return True
    return f" {w} " in f" {text.lower()} "


def guess_name_from_intro(transcript: str) -> str | None:
    """Find name after i'm / i am / my name is / this is."""
    lower = transcript.lower()
    for prefix in ("i'm ", "i am ", "my name is ", "this is "):
        idx = lower.find(prefix)
        if idx < 0:
            continue
        words = []
        for w in transcript[idx + len(prefix) :].split():
            if not w or not w[0].isupper():
                break
            words.append(w.strip(".,!?;:"))
            if len(words) >= 4:
                break
        return " ".join(words) if words else None
    return None

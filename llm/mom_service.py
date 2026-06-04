"""
MOM generation: Groq API (preferred) → Ollama (local) → simple text fallback.

Groq:
  export GROQ_API_KEY=your_key
  export GROQ_MODEL=llama-3.1-8b-instant   # optional

Ollama (if no Groq key):
  ollama pull qwen2:0.5b
  export OLLAMA_MODEL=qwen2:0.5b
"""

import os

import httpx

from utils.speaker_names import filter_person_names

GROQ_API_KEY = os.environ.get("GROQ_API_KEY", "").strip()
GROQ_MODEL = os.environ.get("GROQ_MODEL", "llama-3.1-8b-instant")
GROQ_URL = "https://api.groq.com/openai/v1/chat/completions"

OLLAMA_URL = os.environ.get("OLLAMA_BASE_URL", "http://127.0.0.1:11434").rstrip("/")
OLLAMA_MODEL = os.environ.get("OLLAMA_MODEL", "qwen2:0.5b")
OLLAMA_ENABLED = os.environ.get("OLLAMA_ENABLED", "true").lower() not in (
    "0",
    "false",
    "no",
)

TIMEOUT = float(os.environ.get("MOM_TIMEOUT_SEC", "120"))
MAX_CHARS = int(os.environ.get("MOM_MAX_TRANSCRIPT_CHARS", "12000"))

_HEADINGS = (
    "## Summary",
    "## Attendees",
    "## Discussion",
    "## Action Items",
    "## Decisions",
    "## Next Steps",
)

_SYSTEM = """You are a meeting secretary. Write Minutes of Meeting (MOM) in markdown.

Rules:
- Use ONLY facts from the transcript and speaker-wise lines. Do not invent tasks or attendees.
- Attribute discussion to the correct speaker using the "Speaker-wise transcript" section.
- Under ## Discussion use bullets like: * Name: what they said (one bullet per speaker turn or topic).
- Under ## Action Items use: * Name → task (only if clearly stated).
- If unknown, write "* Not mentioned" under that section.
- Do NOT repeat empty section headings.
- Do NOT apologize, correct typos, or comment on transcription quality.
- Output exactly these sections once, in order: Summary, Attendees, Discussion, Action Items, Decisions, Next Steps.
- Each section must have real content (not only the heading line).
- No # Meeting MOM title."""


def generate_mom(transcript: str) -> str:
    return _generate_mom(transcript, "", [])


def generate_speaker_aware_mom(
    speaker_segments: list[dict],
    participants: list[str] | None = None,
    *,
    transcript: str | None = None,
    speaker_transcript: str | None = None,
) -> str:
    participants = filter_person_names(participants or [])
    speaker_txt = speaker_transcript or _speaker_lines(speaker_segments)
    full = (transcript or "").strip() or " ".join(
        str(s.get("text", "")).strip()
        for s in speaker_segments
        if str(s.get("text", "")).strip()
    )
    return _generate_mom(full, speaker_txt, participants)


def _speaker_lines(segments: list[dict]) -> str:
    return "\n".join(
        f"{s.get('speaker', 'Unknown')}: {s.get('text', '')}"
        for s in segments
        if str(s.get("text", "")).strip()
    )


def _generate_mom(transcript: str, speaker_transcript: str, participants: list[str]) -> str:
    if GROQ_API_KEY:
        try:
            text = _call_groq(transcript, speaker_transcript, participants)
            print(f"MOM generated with Groq ({GROQ_MODEL})")
            return text
        except Exception as e:
            print(f"Groq MOM failed: {e}")

    if OLLAMA_ENABLED:
        try:
            text = _call_ollama(transcript, speaker_transcript, participants)
            print(f"MOM generated with Ollama ({OLLAMA_MODEL})")
            return text
        except Exception as e:
            print(f"Ollama MOM failed: {e}")

    print("Using simple MOM fallback")
    return _minimal_mom(transcript, speaker_transcript, participants)


def _build_user_prompt(
    transcript: str, speaker_transcript: str, participants: list[str]
) -> str:
    names = "\n".join(f"* {p}" for p in participants) if participants else "* (from transcript)"
    return (
        f"Known participants:\n{names}\n\n"
        f"Full transcript:\n{_clip(transcript, MAX_CHARS)}\n\n"
        f"Speaker-wise transcript (use this for who said what):\n"
        f"{_clip(speaker_transcript or '(none)', MAX_CHARS)}\n\n"
        "Write the MOM with sections: ## Summary, ## Attendees, ## Discussion, "
        "## Action Items, ## Decisions, ## Next Steps."
    )


def _clip(text: str, limit: int) -> str:
    text = (text or "").strip()
    return text if len(text) <= limit else text[: limit - 40] + "\n[...truncated...]"


def _chat_completion(messages: list[dict], *, provider: str, url: str, headers: dict, body: dict) -> str:
    response = httpx.post(url, headers=headers, json=body, timeout=TIMEOUT)
    if response.status_code != 200:
        raise RuntimeError(f"{provider} HTTP {response.status_code}: {response.text[:400]}")

    if provider == "Groq":
        raw = (response.json().get("choices") or [{}])[0].get("message", {}).get("content", "")
    else:
        raw = (response.json().get("message") or {}).get("content", "")

    text = _clean_output(raw)
    if not _is_valid_mom(text):
        raise RuntimeError("Model returned empty or invalid MOM sections")
    return text


def _call_groq(transcript: str, speaker_transcript: str, participants: list[str]) -> str:
    return _chat_completion(
        [
            {"role": "system", "content": _SYSTEM},
            {"role": "user", "content": _build_user_prompt(transcript, speaker_transcript, participants)},
        ],
        provider="Groq",
        url=GROQ_URL,
        headers={
            "Authorization": f"Bearer {GROQ_API_KEY}",
            "Content-Type": "application/json",
        },
        body={
            "model": GROQ_MODEL,
            "messages": [
                {"role": "system", "content": _SYSTEM},
                {
                    "role": "user",
                    "content": _build_user_prompt(transcript, speaker_transcript, participants),
                },
            ],
            "temperature": 0.2,
            "max_tokens": 2048,
        },
    )


def _call_ollama(transcript: str, speaker_transcript: str, participants: list[str]) -> str:
    user = _build_user_prompt(transcript, speaker_transcript, participants)
    return _chat_completion(
        [
            {"role": "system", "content": _SYSTEM},
            {"role": "user", "content": user},
        ],
        provider="Ollama",
        url=f"{OLLAMA_URL}/api/chat",
        headers={"Content-Type": "application/json"},
        body={
            "model": OLLAMA_MODEL,
            "messages": [
                {"role": "system", "content": _SYSTEM},
                {"role": "user", "content": user},
            ],
            "stream": False,
            "options": {"temperature": 0.2, "num_predict": 2048},
        },
    )


def _clean_output(raw: str) -> str:
    text = (raw or "").strip()
    if text.lower().startswith("# meeting mom"):
        text = "\n".join(text.splitlines()[1:]).strip()
    if text.startswith("```"):
        lines = text.splitlines()
        if lines and lines[0].startswith("```"):
            lines = lines[1:]
        if lines and lines[-1].strip() == "```":
            lines = lines[:-1]
        text = "\n".join(lines).strip()
    return text


def _is_valid_mom(text: str) -> bool:
    if len(text) < 150:
        return False
    if any(h not in text for h in _HEADINGS):
        return False
    lines = [ln.strip() for ln in text.splitlines() if ln.strip()]
    if not lines:
        return False
    heading_only = sum(1 for ln in lines if ln in _HEADINGS or ln == "## Summary")
    if heading_only >= len(lines) - 2:
        return False
    return True


def _minimal_mom(transcript: str, speaker_transcript: str, participants: list[str]) -> str:
    attendees = "\n".join(f"* {p}" for p in participants) if participants else "* Not mentioned"
    discussion_lines = []
    for line in (speaker_transcript or "").splitlines():
        line = line.strip()
        if line and ":" in line:
            discussion_lines.append(f"* {line[:200]}")
    if not discussion_lines:
        short = (transcript or "")[:400]
        discussion_lines = [f"* {short}"] if short else ["* Not mentioned"]

    summary = discussion_lines[0].replace("* ", "", 1)[:300]

    return f"""## Summary

{summary}

## Attendees

{attendees}

## Discussion

{chr(10).join(discussion_lines[:8])}

## Action Items

* Not mentioned

## Decisions

* Not mentioned

## Next Steps

* Not mentioned"""

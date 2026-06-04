"""
MOM via local Ollama. Regex fallback if Ollama fails.

Low RAM setup (Whisper + Ollama cannot both stay loaded):
  ollama pull qwen2:0.5b
  export OLLAMA_MODEL=qwen2:0.5b

More RAM (8GB+): ollama pull llama3.2:3b && export OLLAMA_MODEL=llama3.2:3b
"""

from __future__ import annotations

import logging
import os
import re

import httpx

from utils.speaker_names import filter_person_names, is_valid_person_name

logger = logging.getLogger(__name__)

OLLAMA_URL = os.environ.get("OLLAMA_BASE_URL", "http://127.0.0.1:11434").rstrip("/")
OLLAMA_MODEL = os.environ.get("OLLAMA_MODEL", "qwen2:0.5b")
OLLAMA_ENABLED = os.environ.get("OLLAMA_ENABLED", "true").lower() not in (
    "0",
    "false",
    "no",
)
TIMEOUT = float(os.environ.get("OLLAMA_TIMEOUT_SEC", "120"))
MAX_CHARS = int(os.environ.get("OLLAMA_MAX_TRANSCRIPT_CHARS", "12000"))

_SECTIONS = (
    "## Summary",
    "## Attendees",
    "## Discussion",
    "## Action Items",
    "## Decisions",
    "## Next Steps",
)

_SYSTEM = """You write meeting minutes from transcripts only.
Rules: use ONLY transcript facts; no invented names or tasks; say "Not mentioned" when unclear.
Output markdown with exactly these headings in order: ## Summary, ## Attendees, ## Discussion, ## Action Items, ## Decisions, ## Next Steps.
Use * bullets. No # Meeting MOM title."""

_FORMAT = """## Summary
(2-4 sentences)
## Attendees
* names
## Discussion
* points
## Action Items
* Person → task
## Decisions
* items or * Not mentioned
## Next Steps
* items or * Not mentioned"""


def generate_mom(transcript: str) -> str:
    return _mom(transcript, "", [])


def generate_speaker_aware_mom(
    speaker_segments: list[dict],
    participants: list[str] | None = None,
    *,
    transcript: str | None = None,
    speaker_transcript: str | None = None,
) -> str:
    participants = filter_person_names(participants or [])
    speaker_txt = speaker_transcript or _lines_from_segments(speaker_segments)
    full = (transcript or "").strip() or " ".join(
        str(s.get("text", "")).strip()
        for s in speaker_segments
        if str(s.get("text", "")).strip()
    )
    return _mom(full, speaker_txt, participants, speaker_segments)


def _lines_from_segments(segments: list[dict]) -> str:
    return "\n".join(
        f"{s.get('speaker', 'Unknown')}: {s.get('text', '')}"
        for s in segments
        if str(s.get("text", "")).strip()
    )


def _mom(
    transcript: str,
    speaker_transcript: str,
    participants: list[str],
    speaker_segments: list[dict] | None = None,
) -> str:
    try:
        return _ollama_mom(transcript, speaker_transcript, participants)
    except Exception as e:
        print(f"Ollama MOM failed, using fallback: {e}")
        if speaker_segments:
            return _fallback_segments(speaker_segments, participants)
        return _fallback_text(transcript)


def _clip(text: str, n: int) -> str:
    text = (text or "").strip()
    return text if len(text) <= n else text[: n - 60] + "\n[...truncated...]"


def _ollama_mom(transcript: str, speaker_transcript: str, participants: list[str]) -> str:
    if not OLLAMA_ENABLED:
        raise RuntimeError("OLLAMA_ENABLED=false")

    names = "\n".join(f"- {p}" for p in participants) or "- (from transcript only)"
    user = f"""Participants:
{names}

Transcript:
{_clip(transcript, MAX_CHARS)}

By speaker:
{_clip(speaker_transcript or "(none)", MAX_CHARS // 2)}

Format:
{_FORMAT}"""

    r = httpx.post(
        f"{OLLAMA_URL}/api/chat",
        json={
            "model": OLLAMA_MODEL,
            "messages": [
                {"role": "system", "content": _SYSTEM},
                {"role": "user", "content": user},
            ],
            "stream": False,
            "options": {"temperature": 0.2, "num_predict": 1200},
        },
        timeout=TIMEOUT,
    )
    if r.status_code != 200:
        err = r.text[:400]
        if "memory" in err.lower():
            raise RuntimeError(
                f"{err} — Free RAM: stop other apps, use smaller model "
                f"(ollama pull qwen2:0.5b; export OLLAMA_MODEL=qwen2:0.5b). "
                "Whisper is unloaded before MOM automatically."
            )
        raise RuntimeError(f"Ollama HTTP {r.status_code}: {err}")

    text = (r.json().get("message") or {}).get("content", "").strip()
    if text.startswith("```"):
        text = re.sub(r"^```\w*\n?", "", text)
        text = re.sub(r"\n?```$", "", text).strip()
    missing = [h for h in _SECTIONS if h not in text]
    if not text or missing:
        raise RuntimeError(f"Invalid MOM sections missing: {missing}")
    print(f"MOM generated with Ollama ({OLLAMA_MODEL})")
    return text


def _fallback_text(transcript: str) -> str:
    t = transcript.strip() or "Not captured"
    short = t[:400] + ("..." if len(t) > 400 else "")
    return f"""## Summary

{short}

## Attendees

* Not mentioned

## Discussion

* {short}

## Action Items

* Not mentioned

## Decisions

* Not mentioned

## Next Steps

* Not mentioned"""


def _fallback_segments(segments: list[dict], participants: list[str]) -> str:
    if not participants:
        for s in segments:
            n = str(s.get("speaker", "")).strip()
            if is_valid_person_name(n) and n not in participants:
                participants.append(n)
    combined = " ".join(str(s.get("text", "")) for s in segments).strip()
    summary = combined[:400] + ("..." if len(combined) > 400 else "") or "Not captured"
    discussion = "\n".join(
        f"* {s.get('speaker', '?')}: {str(s.get('text', ''))[:100]}"
        for s in segments[:6]
        if str(s.get("text", "")).strip()
    )
    attendees = "\n".join(f"* {p}" for p in participants) or "* Unknown"
    return f"""## Summary

{summary}

## Attendees

{attendees}

## Discussion

{discussion or "* Not mentioned"}

## Action Items

* Not mentioned

## Decisions

* Not mentioned

## Next Steps

* Not mentioned"""

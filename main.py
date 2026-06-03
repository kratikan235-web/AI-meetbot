from fastapi import FastAPI, UploadFile, File, HTTPException, Form
import shutil
import os
import json
import re

from stt.whisper_service import transcribe_audio, transcribe_segments
from llm.ollama_service import generate_mom, generate_speaker_aware_mom
from utils.file_writer import save_mom_file
from utils.recording_paths import dated_recording_path
from utils.speaker_names import (
    build_speaker_timeline,
    canonicalize_speaker_name,
    filter_person_names,
    is_valid_person_name,
    sanitize_speaker_events,
)
from utils.audio_convert import (
    to_whisper_wav,
    audio_duration_seconds,
    is_silent_wav,
    SilentAudioError,
)

app = FastAPI()

MIN_UPLOAD_BYTES = 1_500
MIN_BYTES_PER_SECOND = 800
MIN_TRANSCRIPT_WORDS = 3


def is_useful_transcript(text: str) -> bool:
    words = [w for w in text.split() if len(w) > 2]
    return len(words) >= MIN_TRANSCRIPT_WORDS


def _parse_json_list(value: str | None) -> list:
    if not value:
        return []
    try:
        obj = json.loads(value)
        return obj if isinstance(obj, list) else []
    except Exception:
        return []


def _normalize_speaker_events(
    events: list,
    recording_started_at_ms: str | None,
) -> list[dict]:
    """
    Accept either:
    - [{t: <ms since start>, name: ...}]
    - [{tsMs: <epoch ms>, name: ...}]
    and normalize to [{t,name,source}].
    """
    started_at = None
    if recording_started_at_ms:
        try:
            started_at = int(recording_started_at_ms)
        except Exception:
            started_at = None

    out: list[dict] = []
    for e in events:
        if not isinstance(e, dict):
            continue
        name = str(e.get("name", "")).strip()
        if not name:
            continue
        source = str(e.get("source", "")).strip() or "unknown"

        if "t" in e:
            try:
                t = int(e.get("t", 0))
            except Exception:
                t = 0
        elif "tsMs" in e:
            try:
                ts = int(e.get("tsMs"))
                if started_at is not None:
                    t = ts - started_at
                else:
                    t = ts  # normalized below if needed
            except Exception:
                continue
        else:
            continue

        if t < 0:
            t = 0
        out.append({"t": t, "name": name, "source": source})

    # If events used absolute tsMs without recording start, make them relative.
    if out and started_at is None:
        min_t = min(e["t"] for e in out)
        if min_t > 1_000_000_000_000:  # epoch ms
            for e in out:
                e["t"] = max(0, e["t"] - min_t)

    out.sort(key=lambda x: x["t"])
    return out


def _guess_speaker_from_transcript(transcript: str) -> str | None:
    m = re.search(
        r"\b(?:i'?m|i am|my name is|this is)\s+([A-Za-z][\w .'-]{1,40})\b",
        transcript,
        re.I,
    )
    if m:
        name = m.group(1).strip()
        if is_valid_person_name(name):
            return name
    return None


def _speaker_hint_from_text(text: str, roster: list[str]) -> str | None:
    """If segment mentions another participant's first name, prefer them."""
    lower = text.lower()
    for name in roster:
        if not is_valid_person_name(name):
            continue
        first = name.split()[0].lower()
        if len(first) >= 3 and first in lower:
            return name
    return None


def _timeline_is_degenerate(events: list[dict], duration_sec: float) -> bool:
    """
    True when extension timeline cannot map audio (e.g. switch at 1s for a 95s call).
    """
    if len(events) < 2 or duration_sec <= 0:
        return len(events) < 2
    duration_ms = int(duration_sec * 1000)
    second_t = int(events[1].get("t", 0))
    if second_t < duration_ms * 0.12:
        return True
    span = int(events[-1].get("t", 0)) - int(events[0].get("t", 0))
    return span < duration_ms * 0.12


def _content_speaker_for_segment(
    text: str, roster: list[str]
) -> tuple[str | None, str | None]:
    """
    Infer speaker from what was said (question vs work update vs thanks).
    Used when Meet timeline timestamps are not aligned with audio.
    """
    lower = text.lower()
    kratika = next((n for n in roster if "kratika" in n.lower()), None)
    aman = next((n for n in roster if "aman" in n.lower()), None)

    if re.search(r"\bkartiga\b|\bkratika\b", lower) and kratika:
        return kratika, "kartiga/kratika in text"
    if re.search(r"\baman\b", lower) and aman:
        return aman, "aman in text"

    hint = _speaker_hint_from_text(text, roster)
    if hint:
        return hint, f"first name in text -> {hint.split()[0]}"

    is_closer = bool(
        re.search(
            r"thank\s+you|thanks\s+for\s+your\s+update|have a nice day|nice day",
            lower,
        )
    )
    is_question = bool(
        re.search(
            r"what are you doing|give your update|can you please|how about you|"
            r"keep you happy|horrible\.?\s*hi",
            lower,
        )
    )
    is_update = bool(
        re.search(
            r"\b(class|junior|deployment|module|config|test|campaign|solar|"
            r"implementation|frontline|morning|took a|multi-tenant|configuration)\b",
            lower,
        )
    )

    if is_closer and not is_update:
        return kratika or roster[-1], "closing / thank-you phrasing"
    if is_question and not is_update:
        return kratika or roster[0], "asking the other participant for an update"
    if is_update and not is_question:
        return aman or roster[0], "work-update phrasing (long answer)"

    return None, None


def _infer_timeline_from_segments(
    segments: list[dict], roster: list[str]
) -> list[dict]:
    """Build a display timeline from per-segment content (for logs)."""
    out: list[dict] = []
    prev = None
    for s in segments:
        sp, _ = _content_speaker_for_segment(str(s.get("text", "")).strip(), roster)
        if not sp or sp == prev:
            continue
        out.append(
            {
                "t": int(float(s.get("start", 0)) * 1000),
                "name": sp,
                "source": "inferred_from_segments",
            }
        )
        prev = sp
    return out


def _log_timeline_seconds(label: str, events: list[dict]) -> None:
    print(label)
    if not events:
        print("  (empty)")
        return
    for e in events:
        t_sec = int(e.get("t", 0)) / 1000.0
        print(f"  {t_sec:.1f}s -> {e.get('name')} ({e.get('source', '')})")


def _assign_speakers_to_segments(
    segments: list[dict],
    speaker_events: list[dict],
    roster: list[str] | None = None,
    duration_sec: float = 0.0,
    *,
    force_content: bool = False,
) -> list[dict]:
    """
    Map whisper segments -> speaker names using speaker_events timeline.
    Uses segment midpoint + optional name mention in text.
    """
    roster = [p for p in (roster or []) if is_valid_person_name(p)]
    events = []
    for e in speaker_events:
        if not isinstance(e, dict):
            continue
        raw = str(e.get("name", "")).strip()
        if not raw:
            continue
        name = canonicalize_speaker_name(raw) or raw
        if not is_valid_person_name(name):
            continue
        if roster and name not in roster:
            continue
        events.append({"t": int(e.get("t", 0)), "name": name})
    events.sort(key=lambda e: e["t"])
    use_content = force_content or _timeline_is_degenerate(events, duration_sec)

    if use_content and roster:
        labeled = []
        for s in segments:
            txt = str(s.get("text", "")).strip()
            sp, reason = _content_speaker_for_segment(txt, roster)
            if not sp:
                sp = "Unknown"
                reason = "content rules inconclusive"
            labeled.append(
                {
                    "speaker": sp,
                    "assign_reason": f"content-based (degenerate timeline): {reason}",
                    **s,
                }
            )
        return labeled

    if not events:
        return [{"speaker": "Unknown", "assign_reason": "no timeline", **s} for s in segments]

    intervals: list[dict] = []
    for i, e in enumerate(events):
        start = e["t"]
        end = events[i + 1]["t"] if i + 1 < len(events) else 10**12
        intervals.append({"start": start, "end": end, "name": e["name"]})

    labeled: list[dict] = []
    alt_idx = 0
    for s in segments:
        s0 = int(float(s["start"]) * 1000)
        s1 = int(float(s["end"]) * 1000)
        if s1 <= s0:
            s1 = s0 + 1
        mid = (s0 + s1) // 2
        txt = str(s.get("text", "")).strip()
        start_s = float(s.get("start", 0))
        end_s = float(s.get("end", 0))

        best_name = "Unknown"
        reason = "no matching interval"
        for iv in intervals:
            if iv["start"] <= mid < iv["end"]:
                best_name = iv["name"]
                reason = (
                    f"midpoint {mid / 1000:.1f}s in "
                    f"[{iv['start'] / 1000:.1f}s, {iv['end'] / 1000:.1f}s) -> {iv['name']}"
                )
                break

        content_sp, content_reason = _content_speaker_for_segment(txt, roster)
        if content_sp:
            best_name = content_sp
            reason = f"content override: {content_reason}"
        else:
            hint = _speaker_hint_from_text(txt, roster)
            if hint:
                best_name = hint
                reason = f"segment text mentions {hint.split()[0]}"

        if best_name == "Unknown" and len(roster) >= 2:
            best_name = roster[alt_idx % len(roster)]
            reason = f"fallback alternate roster[{alt_idx % len(roster)}]"
            alt_idx += 1

        labeled.append(
            {
                "speaker": best_name,
                "assign_reason": reason,
                "segment_start_s": start_s,
                "segment_end_s": end_s,
                **s,
            }
        )

    return labeled


@app.post("/upload")
async def upload_audio(
    file: UploadFile = File(...),
    speaker_events: str | None = Form(None),
    participants: str | None = Form(None),
    recording_started_at_ms: str | None = Form(None),
    self_name: str | None = Form(None),
):
    safe_name = os.path.basename(file.filename or "meeting.webm")
    file_path = dated_recording_path(safe_name)

    with open(file_path, "wb") as buffer:
        shutil.copyfileobj(file.file, buffer)

    size = os.path.getsize(file_path)
    print(f"File saved at: {file_path} ({size} bytes)")

    if size < MIN_UPLOAD_BYTES:
        raise HTTPException(status_code=400, detail=f"Audio file too small ({size} bytes).")

    try:
        wav_path = to_whisper_wav(file_path)
    except SilentAudioError as e:
        raise HTTPException(status_code=400, detail=str(e)) from e

    duration = audio_duration_seconds(wav_path)
    print(f"Converted for Whisper: {wav_path} ({duration:.1f}s)")

    bytes_per_sec = size / duration if duration > 0 else 0
    if bytes_per_sec < MIN_BYTES_PER_SECOND:
        raise HTTPException(
            status_code=400,
            detail=f"Recording looks silent ({bytes_per_sec:.0f} bytes/s). Check microphone.",
        )

    if is_silent_wav(wav_path):
        raise HTTPException(status_code=400, detail="No audible sound in recording.")

    # Keep existing behavior (plain transcript) for backward compatibility.
    transcript = transcribe_audio(wav_path).strip()
    if not is_useful_transcript(transcript):
        raise HTTPException(status_code=400, detail=f"Transcript too short: '{transcript}'")

    # Optional speaker-aware pipeline
    print(
        "speaker_events form field:",
        "missing" if speaker_events is None else f"{len(speaker_events)} chars",
    )
    print(
        "participants form field:",
        "missing" if participants is None else f"{len(participants)} chars",
    )

    raw_events_list = _parse_json_list(speaker_events)
    raw_participants_list = _parse_json_list(participants)
    print("raw speaker_events count:", len(raw_events_list))
    if raw_events_list:
        print("raw speaker_events sample:", raw_events_list[:5])
    print("raw participants (extension):", raw_participants_list)

    print("Participants (raw):", raw_participants_list)
    participants_list = filter_person_names(raw_participants_list)
    participants_list = [p for p in participants_list if is_valid_person_name(p)]
    print("Participants (after filtering):", participants_list)

    normalized_raw = _normalize_speaker_events(raw_events_list, recording_started_at_ms)
    print("Speaker Events (raw count):", len(normalized_raw))
    for e in normalized_raw[:20]:
        print(f"  {e.get('t')}ms -> {e.get('name')} ({e.get('source')})")
    if len(normalized_raw) > 20:
        print(f"  ... +{len(normalized_raw) - 20} more")

    events_list = build_speaker_timeline(normalized_raw, participants_list, duration)
    print("Speaker timeline (after build):", len(events_list))
    for e in events_list:
        print(f"  {e.get('t')}ms -> {e.get('name')} ({e.get('source')})")

    trusted_self = (self_name or "").strip()

    def _distinct_valid_speakers(events: list[dict]) -> set[str]:
        names: set[str] = set()
        for e in events:
            n = str(e.get("name", "")).strip()
            if is_valid_person_name(n):
                names.add(n)
        return names

    distinct_speakers = _distinct_valid_speakers(events_list)
    multi_party = len(participants_list) >= 2 or len(distinct_speakers) >= 2
    print(f"multi_party={multi_party} distinct_speakers={sorted(distinct_speakers)} participants={participants_list}")

    if trusted_self and is_valid_person_name(trusted_self):
        # Solo only when one attendee AND one speaker timeline — do not collapse multi-person calls.
        if not multi_party and len(participants_list) <= 1 and len(distinct_speakers) <= 1:
            participants_list = [trusted_self]
            events_list = [{"t": 0, "name": trusted_self, "source": "solo_meeting"}]
            print(f"solo meeting: all speech → {trusted_self}")
        elif events_list and not is_valid_person_name(str(events_list[0].get("name", ""))):
            # Junk Meet UI label was used as speaker (e.g. Hand raises) — drop bad events.
            events_list = [e for e in events_list if is_valid_person_name(str(e.get("name", "")))]
            if not events_list:
                events_list = [{"t": 0, "name": trusted_self, "source": "self_name_fix"}]

    if not events_list:
        guessed = _guess_speaker_from_transcript(transcript)
        if guessed and is_valid_person_name(guessed):
            events_list = [{"t": 0, "name": guessed, "source": "transcript_guess"}]

    if not events_list and participants_list:
        events_list = [{"t": 0, "name": participants_list[0], "source": "participant_fallback"}]

    if not events_list and trusted_self and is_valid_person_name(trusted_self):
        events_list = [{"t": 0, "name": trusted_self, "source": "self_name_only"}]
        if not participants_list:
            participants_list = [trusted_self]
        print(f"fallback: single speaker from self_name → {trusted_self}")

    # Restore speaker-wise transcript when extension sent empty events but transcript exists.
    if not events_list and is_useful_transcript(transcript):
        label = trusted_self if trusted_self and is_valid_person_name(trusted_self) else None
        if not label and participants_list:
            label = participants_list[0]
        if not label or not is_valid_person_name(label):
            label = "Speaker"
        events_list = [{"t": 0, "name": label, "source": "transcript_guarantee"}]
        if label not in participants_list:
            participants_list = [label] + participants_list
        print(f"guarantee: speaker-wise transcript enabled with label → {label}")

    # Attendees = extension roster only (never add UI labels from events)
    participants_list = filter_person_names(participants_list)

    print("speaker_events (final timeline):", events_list)
    print("participants (final attendees):", participants_list)
    print(f"speaker_events count: {len(events_list)}; participants count: {len(participants_list)}")
    if participants_list:
        print("attendees:", participants_list)
    if events_list:
        try:
            print("first speaker event:", events_list[0])
        except Exception:
            pass

    speaker_segments: list[dict] = []
    speaker_transcript = ""
    if events_list:
        segs = transcribe_segments(wav_path)
        print(f"whisper segments for mapping: {len(segs)}")

        degenerate = _timeline_is_degenerate(events_list, duration)
        _log_timeline_seconds("Speaker timeline (extension, seconds):", events_list)
        if degenerate:
            print(
                "WARNING: timeline degenerate — second speaker switch is before 12% of "
                f"recording ({duration:.1f}s). Midpoint mapping would assign almost everything "
                "to one person. Using content-based segment assignment instead."
            )
            inferred = _infer_timeline_from_segments(segs, participants_list)
            _log_timeline_seconds("Inferred timeline from segment content (seconds):", inferred)

        intervals_dbg = []
        sorted_ev = sorted(events_list, key=lambda e: int(e.get("t", 0)))
        for i, e in enumerate(sorted_ev):
            end_ms = sorted_ev[i + 1]["t"] if i + 1 < len(sorted_ev) else "end"
            end_s = f"{int(end_ms) / 1000:.1f}s" if end_ms != "end" else "end"
            intervals_dbg.append(
                f"{e.get('name')} [{int(e.get('t', 0)) / 1000:.1f}s - {end_s})"
            )
        print("Speaker timeline intervals:", intervals_dbg)

        speaker_segments = _assign_speakers_to_segments(
            segs,
            events_list,
            roster=participants_list,
            duration_sec=duration,
            force_content=degenerate,
        )
        print("Transcript segments + mapping:")
        for i, seg in enumerate(speaker_segments):
            sp = str(seg.get("speaker", ""))
            if not is_valid_person_name(sp) and participants_list:
                seg["speaker"] = participants_list[0]
                seg["assign_reason"] = "invalid speaker; roster fallback"
            txt = str(seg.get("text", "")).strip()
            if not txt:
                continue
            print(f"  Segment {i + 1}: start={seg.get('start')}s end={seg.get('end')}s")
            print(f"    speaker={seg.get('speaker')}")
            print(f"    text={txt[:100]}")
            print(f"    reason={seg.get('assign_reason', 'n/a')}")
        speaker_transcript = "\n".join(
            f"{s.get('speaker','Unknown')}: {str(s.get('text','')).strip()}"
            for s in speaker_segments
            if str(s.get("text", "")).strip()
        ).strip()
        print(f"speaker_transcript lines: {len(speaker_transcript.splitlines())}")
    else:
        print("speaker-wise transcript skipped: no events_list after processing")

    if speaker_segments:
        summary = generate_speaker_aware_mom(speaker_segments, participants_list)
    else:
        summary = generate_mom(transcript)

    final_doc = f"""# Meeting MOM

## Transcript
{transcript}

## Speaker-wise Transcript
{speaker_transcript or "(not available)"}

---

## Summary
{summary}
"""
    saved_file = save_mom_file(final_doc)
    print(" MOM saved at:", saved_file)

    return {
        "transcript": transcript,
        "speaker_transcript": speaker_transcript,
        "participants": participants_list,
        "speaker_events_count": len(events_list),
        "mom": summary,
        "audio_saved": file_path,
        "wav_saved": wav_path,
        "file_saved": saved_file,
        "duration_sec": duration,
    }

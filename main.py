from fastapi import FastAPI, UploadFile, File, HTTPException, Form
import shutil
import os
import json
import re

from stt.whisper_service import transcribe_audio, transcribe_segments, unload_model
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


_TRANSITION_WINDOW_MS = 3000


def _addressed_other_speaker(
    text: str, roster: list[str]
) -> tuple[str | None, str | None]:
    """Speaker talking TO someone named in the text (not the named person)."""
    lower = text.lower()
    for name in roster:
        if not is_valid_person_name(name):
            continue
        first = name.split()[0].lower()
        if len(first) < 3:
            continue
        others = [n for n in roster if n != name]
        if len(others) != 1:
            continue
        if re.search(rf"\b(?:hello|hi|hey)\s+{re.escape(first)}\b", lower):
            return others[0], f"greeting directed at {first} (speaker is the other participant)"
        if re.search(rf"\b{re.escape(first)}\b", lower) and re.search(
            r"\b(?:share your|give your|your update|updates again)\b",
            lower,
        ):
            return others[0], f"asking {first} for an update (speaker is the other participant)"
    return None, None


def _nearest_event_before(events: list[dict], t_ms: int) -> dict:
    before = [e for e in events if int(e.get("t", 0)) <= t_ms]
    return before[-1] if before else events[0]


def _nearest_event_after(events: list[dict], t_ms: int) -> dict:
    after = [e for e in events if int(e.get("t", 0)) >= t_ms]
    return after[0] if after else events[-1]


def _pick_speaker_from_timeline(
    events: list[dict],
    s0_ms: int,
    s1_ms: int,
    mid_ms: int,
    *,
    is_first: bool,
    is_last: bool,
) -> tuple[str, str]:
    """Nearest speaker events at start/mid/end; wider window on first/last segments."""
    votes: dict[str, int] = {}
    details: list[str] = []

    for t_ms, label in ((s0_ms, "start"), (mid_ms, "mid"), (max(s1_ms - 1, s0_ms), "end")):
        before = _nearest_event_before(events, t_ms)
        votes[before["name"]] = votes.get(before["name"], 0) + 2
        details.append(
            f"before {label} ({t_ms / 1000:.1f}s): {before['name']} at {before['t'] / 1000:.1f}s"
        )
        if is_first or is_last:
            after = _nearest_event_after(events, t_ms)
            if abs(int(after["t"]) - t_ms) <= _TRANSITION_WINDOW_MS:
                votes[after["name"]] = votes.get(after["name"], 0) + 1
                details.append(
                    f"after {label} ({t_ms / 1000:.1f}s): {after['name']} at {after['t'] / 1000:.1f}s"
                )

    winner = max(votes, key=lambda k: votes[k])
    return winner, f"timeline vote ({votes[winner]} pts): " + "; ".join(details[:4])


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


def _host_from_roster(roster: list[str], host_name: str | None) -> str | None:
    if host_name and is_valid_person_name(host_name) and host_name in roster:
        return host_name
    return None


def _first_non_host(roster: list[str], host: str | None) -> str | None:
    if host:
        others = [n for n in roster if n != host]
        return others[0] if others else None
    return roster[0] if roster else None


def _roster_names_mentioned_in_text(
    text_lower: str, roster: list[str], *, skip: set[str] | None = None
) -> list[str]:
    """Roster members whose first name appears as a word in the transcript."""
    skip = skip or set()
    mentioned: list[str] = []
    for name in roster:
        if name in skip or not is_valid_person_name(name):
            continue
        first = name.split()[0].lower()
        if len(first) < 3:
            continue
        if re.search(rf"\b{re.escape(first)}\b", text_lower):
            mentioned.append(name)
    return mentioned


def _content_speaker_for_segment(
    text: str,
    roster: list[str],
    prev_speaker: str | None = None,
    host_name: str | None = None,
) -> tuple[str | None, str | None]:
    """
    Infer speaker from what was said (question vs work update vs thanks).
    Used when Meet timeline timestamps are not aligned with audio.
    """
    lower = text.lower()
    host = _host_from_roster(roster, host_name)

    addressed, addr_reason = _addressed_other_speaker(text, roster)
    if addressed:
        return addressed, addr_reason

    if host and re.search(r"hello everyone|we can start now|can start now", lower):
        return host, "host opening the meeting"

    if host and re.search(r"now you can continue|you can continue|yes,?\s*sure", lower):
        return host, "host yielding or confirming turn"

    if re.search(r"could i start", lower):
        asker = _first_non_host(roster, host)
        if asker:
            return asker, "participant asking permission to start"

    if host:
        host_first = host.split()[0].lower()
        if len(host_first) >= 3 and re.search(
            rf"\b{re.escape(host_first)}\b.{0,60}(?:hosting|initiating)", lower
        ):
            others = [n for n in roster if n != host]
            if len(others) == 1:
                return others[0], "describing host (speaker is not the host)"
            if len(others) >= 2:
                commenters = _roster_names_mentioned_in_text(lower, others, skip={host})
                if commenters:
                    return commenters[0], "side comment about host"
                return others[0], "side comment about host"

    is_thanks = bool(
        re.search(r"thank\s+you|thanks\s+for\s+your\s+update", lower)
    )
    is_farewell = bool(re.search(r"have a nice day|nice day", lower))
    is_question = bool(
        re.search(
            r"what are you doing|give your update|can you please|how about you|"
            r"keep you happy|horrible\.?\s*hi|share your update",
            lower,
        )
    )
    is_update = bool(
        re.search(
            r"\b(class|junior|deployment|module|config|test|campaign|solar|"
            r"implementation|frontline|morning|took a|multi-tenant|configuration|"
            r"portfolio|vecta|django)\b",
            lower,
        )
    )

    if is_thanks and not is_update:
        return host or (roster[-1] if roster else None), "thank-you / acknowledgment"
    if is_farewell and not is_thanks and not is_update and prev_speaker and len(roster) >= 2:
        other = next((n for n in roster if n != prev_speaker), None)
        if other:
            return other, f"farewell after {prev_speaker.split()[0]} spoke"
    if is_question and not is_update and not re.search(r"could i start", lower):
        return host or (roster[0] if roster else None), "facilitator question to group"
    if is_update and not is_question:
        updater = _first_non_host(roster, host) or (roster[0] if roster else None)
        if updater:
            return updater, "work-update phrasing (long answer)"

    mentioned = _roster_names_mentioned_in_text(lower, roster)
    if mentioned and not is_update:
        skip_host_desc = {host} if host else set()
        for name in mentioned:
            if name in skip_host_desc and re.search(r"hosting|initiating", lower):
                continue
            return name, f"{name.split()[0]} mentioned in text"

    return None, None


def _assign_speakers_to_segments(
    segments: list[dict],
    speaker_events: list[dict],
    roster: list[str] | None = None,
    duration_sec: float = 0.0,
    *,
    force_content: bool = False,
    host_name: str | None = None,
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
        prev_speaker = None
        for s in segments:
            txt = str(s.get("text", "")).strip()
            sp, _ = _content_speaker_for_segment(
                txt, roster, prev_speaker=prev_speaker, host_name=host_name
            )
            if not sp:
                sp = "Unknown"
            labeled.append({"speaker": sp, **s})
            if sp != "Unknown":
                prev_speaker = sp
        return labeled

    if not events:
        return [{"speaker": "Unknown", **s} for s in segments]

    labeled: list[dict] = []
    alt_idx = 0
    prev_speaker = None
    n_seg = len(segments)
    for idx, s in enumerate(segments):
        s0 = int(float(s["start"]) * 1000)
        s1 = int(float(s["end"]) * 1000)
        if s1 <= s0:
            s1 = s0 + 1
        mid = (s0 + s1) // 2
        txt = str(s.get("text", "")).strip()

        best_name = "Unknown"
        addressed, _ = _addressed_other_speaker(txt, roster)
        if addressed:
            best_name = addressed
        else:
            best_name, _ = _pick_speaker_from_timeline(
                events,
                s0,
                s1,
                mid,
                is_first=(idx == 0),
                is_last=(idx == n_seg - 1),
            )
            content_sp, _ = _content_speaker_for_segment(
                txt, roster, prev_speaker=prev_speaker, host_name=host_name
            )
            if content_sp:
                best_name = content_sp

        if best_name == "Unknown" and len(roster) >= 2:
            best_name = roster[alt_idx % len(roster)]
            alt_idx += 1

        labeled.append({"speaker": best_name, **s})
        prev_speaker = best_name

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
    print(f"Audio recorded: {file_path}")
    if size < MIN_UPLOAD_BYTES:
        raise HTTPException(status_code=400, detail=f"Audio file too small ({size} bytes).")

    try:
        wav_path = to_whisper_wav(file_path)
    except SilentAudioError as e:
        raise HTTPException(status_code=400, detail=str(e)) from e

    duration = audio_duration_seconds(wav_path)
    print(f"Converted: {wav_path}")

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

    raw_events_list = _parse_json_list(speaker_events)
    raw_participants_list = _parse_json_list(participants)
    trusted_self = (self_name or "").strip()

    participants_list = filter_person_names(raw_participants_list)
    participants_list = [p for p in participants_list if is_valid_person_name(p)]
    if trusted_self and is_valid_person_name(trusted_self):
        if trusted_self not in participants_list:
            participants_list = [trusted_self] + participants_list

    normalized_raw = _normalize_speaker_events(raw_events_list, recording_started_at_ms)
    events_list = build_speaker_timeline(normalized_raw, participants_list, duration)

    def _distinct_valid_speakers(events: list[dict]) -> set[str]:
        names: set[str] = set()
        for e in events:
            n = str(e.get("name", "")).strip()
            if is_valid_person_name(n):
                names.add(n)
        return names

    distinct_speakers = _distinct_valid_speakers(events_list)
    multi_party = len(participants_list) >= 2 or len(distinct_speakers) >= 2

    if trusted_self and is_valid_person_name(trusted_self):
        # Solo only when one attendee AND one speaker timeline — do not collapse multi-person calls.
        if not multi_party and len(participants_list) <= 1 and len(distinct_speakers) <= 1:
            participants_list = [trusted_self]
            events_list = [{"t": 0, "name": trusted_self, "source": "solo_meeting"}]
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

    participants_list = filter_person_names(participants_list)
    if trusted_self and is_valid_person_name(trusted_self) and trusted_self not in participants_list:
        participants_list = [trusted_self] + participants_list

    print(
        f"Participants: {', '.join(participants_list) if participants_list else '(none)'}"
    )

    speaker_segments: list[dict] = []
    speaker_transcript = ""

    if events_list:
        segs = transcribe_segments(wav_path)
        degenerate = _timeline_is_degenerate(events_list, duration)
        host_for_mapping = trusted_self if is_valid_person_name(trusted_self) else None

        speaker_segments = _assign_speakers_to_segments(
            segs,
            events_list,
            roster=participants_list,
            duration_sec=duration,
            force_content=degenerate,
            host_name=host_for_mapping,
        )
        for seg in speaker_segments:
            sp = str(seg.get("speaker", ""))
            if not is_valid_person_name(sp) and participants_list:
                seg["speaker"] = participants_list[0]

        speaker_transcript = "\n".join(
            f"{s.get('speaker','Unknown')}: {str(s.get('text','')).strip()}"
            for s in speaker_segments
            if str(s.get("text", "")).strip()
        ).strip()

    # Free Whisper RAM so Ollama can load on low-memory machines (same upload request).
    unload_model()

    if speaker_segments:
        summary = generate_speaker_aware_mom(
            speaker_segments,
            participants_list,
            transcript=transcript,
            speaker_transcript=speaker_transcript,
        )
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
    print(f"MOM saved: {saved_file}")

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

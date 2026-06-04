from fastapi import FastAPI, UploadFile, File, HTTPException, Form
import shutil
import os
import json

from stt.whisper_service import transcribe_audio, transcribe_segments, unload_model
from llm.mom_service import generate_mom, generate_speaker_aware_mom
from utils.file_writer import save_mom_file
from utils.recording_paths import dated_recording_path
from utils.speaker_content import (
    content_speaker_for_segment,
    guess_speaker_from_transcript,
    addressed_other_speaker,
)
from utils.speaker_names import (
    build_speaker_timeline,
    canonicalize_speaker_name,
    events_for_segment_mapping,
    filter_person_names,
    is_valid_person_name,
    other_speaker_proven_in_captions,
    sanitize_speaker_events,
    transcript_is_recorder_monologue,
    _CAPTION_SOURCES,
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


_TRANSITION_WINDOW_MS = 3000


def _speaker_for_segment_time(
    events: list[dict],
    t_ms: int,
    recorder: str,
    roster: list[str],
) -> str:
    """
    Default to recorder (mic). Assign someone else only if captions prove they spoke near t_ms.
    """
    roster_set = set(roster)
    window_start = max(0, t_ms - 2000)
    window_end = t_ms + 2000
    in_window = [e for e in events if window_start <= int(e.get("t", 0)) <= window_end]

    for e in in_window:
        if e.get("source") not in _CAPTION_SOURCES:
            continue
        name = e.get("name", "")
        if name in roster_set and name != recorder:
            return name

    if recorder and recorder in roster_set:
        return recorder
    if in_window:
        return in_window[-1].get("name", "Unknown")
    if events:
        return _nearest_event_before(events, t_ms)["name"]
    return roster[0] if roster else "Unknown"


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
    """Pick speaker at segment start (primary), with light mid/end checks."""
    before_start = _nearest_event_before(events, s0_ms)
    votes: dict[str, int] = {before_start["name"]: 4}
    details = [f"start ({s0_ms / 1000:.1f}s): {before_start['name']}"]

    before_mid = _nearest_event_before(events, mid_ms)
    votes[before_mid["name"]] = votes.get(before_mid["name"], 0) + 1

    if is_first or is_last:
        after_start = _nearest_event_after(events, s0_ms)
        if abs(int(after_start["t"]) - s0_ms) <= _TRANSITION_WINDOW_MS:
            votes[after_start["name"]] = votes.get(after_start["name"], 0) + 1

    winner = max(votes, key=lambda k: votes[k])
    return winner, f"timeline: {winner} (" + "; ".join(details[:2]) + ")"


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


def _assign_speakers_to_segments(
    segments: list[dict],
    speaker_events: list[dict],
    roster: list[str] | None = None,
    duration_sec: float = 0.0,
    *,
    force_content: bool = False,
    host_name: str | None = None,
    recorder_name: str | None = None,
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
        events.append({"t": int(e.get("t", 0)), "name": name, "source": str(e.get("source", ""))})
    all_events = list(events)
    events = events_for_segment_mapping(events)
    use_content = force_content or _timeline_is_degenerate(events, duration_sec)
    recorder = (recorder_name or host_name or "").strip()
    full_text = " ".join(str(s.get("text", "")).strip() for s in segments)

    if (
        recorder
        and recorder in roster
        and transcript_is_recorder_monologue(full_text)
        and not other_speaker_proven_in_captions(
            [e for e in all_events if e.get("source") in _CAPTION_SOURCES],
            recorder,
            set(roster),
        )
    ):
        return [{"speaker": recorder, **s} for s in segments]

    if recorder and recorder in roster and len(roster) >= 2:
        use_recorder_default = True
    else:
        use_recorder_default = False

    if use_content and roster:
        labeled = []
        prev_speaker = None
        for s in segments:
            txt = str(s.get("text", "")).strip()
            sp, _ = content_speaker_for_segment(
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
    prev_speaker = None
    n_seg = len(segments)
    use_content_override = force_content

    for idx, s in enumerate(segments):
        s0 = int(float(s["start"]) * 1000)
        s1 = int(float(s["end"]) * 1000)
        if s1 <= s0:
            s1 = s0 + 1
        mid = (s0 + s1) // 2
        txt = str(s.get("text", "")).strip()

        if use_recorder_default:
            best_name = _speaker_for_segment_time(all_events, s0, recorder, roster)
        else:
            best_name = "Unknown"
            addressed, _ = addressed_other_speaker(txt, roster)
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
                if use_content_override or best_name == "Unknown":
                    content_sp, _ = content_speaker_for_segment(
                        txt, roster, prev_speaker=prev_speaker, host_name=host_name
                    )
                    if content_sp:
                        best_name = content_sp

            if best_name == "Unknown":
                if recorder and recorder in roster:
                    best_name = recorder
                elif prev_speaker:
                    best_name = prev_speaker
                elif events:
                    best_name = _nearest_event_before(events, s0)["name"]
                elif roster:
                    best_name = roster[0]

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
    events_list = build_speaker_timeline(
        normalized_raw, participants_list, duration, recorder_name=trusted_self
    )
    if (
        trusted_self
        and is_valid_person_name(trusted_self)
        and transcript_is_recorder_monologue(transcript)
        and len({e.get("name") for e in events_list}) == 1
        and trusted_self not in {e.get("name") for e in events_list}
        and not other_speaker_proven_in_captions(
            [e for e in normalized_raw if isinstance(e, dict) and e.get("source") in _CAPTION_SOURCES],
            trusted_self,
            set(participants_list),
        )
    ):
        events_list = [{"t": 0, "name": trusted_self, "source": "recorder_monologue"}]
        print(f"Speaker timeline: recorder monologue -> {trusted_self}")

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
        guessed = guess_speaker_from_transcript(transcript)
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
            recorder_name=trusted_self,
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

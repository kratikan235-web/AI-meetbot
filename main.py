from fastapi import FastAPI, UploadFile, File, HTTPException
import shutil
import os

from stt.whisper_service import transcribe_audio
from llm.ollama_service import generate_mom
from utils.file_writer import save_mom_file
from utils.recording_paths import dated_recording_path
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


@app.post("/upload")
async def upload_audio(file: UploadFile = File(...)):
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

    transcript = transcribe_audio(wav_path).strip()
    if not is_useful_transcript(transcript):
        raise HTTPException(status_code=400, detail=f"Transcript too short: '{transcript}'")

    summary = generate_mom(transcript)

    final_doc = f"""# Meeting MOM

## Transcript
{transcript}

---

## Summary
{summary}
"""
    saved_file = save_mom_file(final_doc)
    print(" MOM saved at:", saved_file)

    return {
        "transcript": transcript,
        "mom": summary,
        "audio_saved": file_path,
        "wav_saved": wav_path,
        "file_saved": saved_file,
        "duration_sec": duration,
    }

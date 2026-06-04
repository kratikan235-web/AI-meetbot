import gc

from faster_whisper import WhisperModel

_model: WhisperModel | None = None


def _get_model() -> WhisperModel:
    global _model
    if _model is None:
        _model = WhisperModel("base")
    return _model


def unload_model() -> None:
    """Free RAM before Ollama MOM (Whisper and LLM do not fit together on low-memory machines)."""
    global _model
    if _model is not None:
        del _model
        _model = None
        gc.collect()


def transcribe_audio(file_path: str) -> str:
    segments, _ = _get_model().transcribe(
        file_path,
        vad_filter=False,
        beam_size=5,
        best_of=5,
        temperature=0.0,
        condition_on_previous_text=False,
    )

    text = ""
    for segment in segments:
        text += segment.text + " "

    return text.strip()


def transcribe_segments(file_path: str) -> list[dict]:
    """
    Whisper transcription with timestamps.

    Returns: [{ "start": float_seconds, "end": float_seconds, "text": str }, ...]
    """
    segments, _ = _get_model().transcribe(
        file_path,
        vad_filter=False,
        beam_size=5,
        best_of=5,
        temperature=0.0,
        condition_on_previous_text=False,
    )

    out: list[dict] = []
    for seg in segments:
        txt = (seg.text or "").strip()
        if not txt:
            continue
        out.append({"start": float(seg.start), "end": float(seg.end), "text": txt})
    return out

from faster_whisper import WhisperModel

model = WhisperModel("base")


def transcribe_audio(file_path: str) -> str:
    segments, _ = model.transcribe(
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

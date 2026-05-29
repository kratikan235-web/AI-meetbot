from faster_whisper import WhisperModel

# load model once
model = WhisperModel("base")

def transcribe_audio(file_path):
    print("Transcribing audio...")

    segments, _ = model.transcribe(file_path)

    text = ""
    for segment in segments:
        text += segment.text + " "

    print("Transcription done")

    return text
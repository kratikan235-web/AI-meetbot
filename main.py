from fastapi import FastAPI
from audio.recorder import record_audio
from stt.whisper_service import transcribe_audio
from llm.ollama_service import generate_mom
from utils.file_writer import save_mom_file

app = FastAPI()

@app.get("/")
def home():
    return {"message": "Server is running "}

# Record API
# @app.get("/record")
# def record():
#     file_path = record_audio(10)  # record 10 seconds
#     return {"file_saved": file_path}

# To Record and Transcribe API
# @app.get("/transcribe")
# def transcribe():
#     file_path = record_audio(10)  # record 10 seconds
#     text = transcribe_audio(file_path)

#     return {
#         "file": file_path,
#         "transcript": text
#     }

# Record, transribe, generate MOM API
@app.get("/mom")
def mom():
    file_path = record_audio(20)
    transcript = transcribe_audio(file_path)
    summary = generate_mom(transcript)

    # format final document
    final_doc = f"""
# Meeting MOM

## Transcript
{transcript}

---

## Summary
{summary}
"""
    saved_file = save_mom_file(final_doc)

    return {
        "transcript": transcript,
        "mom": summary,
        "file_saved": saved_file
    }
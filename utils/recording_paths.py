import os
from datetime import datetime


def dated_recording_path(original_filename: str) -> str:
    """recordings/YYYY-MM-DD/meeting_HH-MM-SS.ext"""
    now = datetime.now()
    date_folder = now.strftime("%Y-%m-%d")
    time_stamp = now.strftime("%H-%M-%S")
    ext = os.path.splitext(original_filename or "meeting.webm")[1] or ".webm"

    session_folder = os.path.join("recordings", date_folder)
    os.makedirs(session_folder, exist_ok=True)

    return os.path.join(session_folder, f"meeting_{time_stamp}{ext}")

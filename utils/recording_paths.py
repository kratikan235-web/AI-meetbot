import os
from datetime import datetime


def date_folder_name(when: datetime | None = None) -> str:
    """Per-day folder label, e.g. '03 june'."""
    when = when or datetime.now()
    return when.strftime("%d %B").lower()


def dated_session_dir(base_dir: str, when: datetime | None = None) -> str:
    """Create and return base_dir/<day month>/ (e.g. mom_reports/03 june/)."""
    folder = os.path.join(base_dir, date_folder_name(when))
    os.makedirs(folder, exist_ok=True)
    return folder


def dated_recording_path(original_filename: str) -> str:
    """recordings/<03 june>/meeting_HH-MM-SS.ext"""
    now = datetime.now()
    time_stamp = now.strftime("%H-%M-%S")
    ext = os.path.splitext(original_filename or "meeting.webm")[1] or ".webm"

    session_folder = dated_session_dir("recordings", now)
    return os.path.join(session_folder, f"meeting_{time_stamp}{ext}")

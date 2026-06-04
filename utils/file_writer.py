import os
from datetime import datetime

from utils.recording_paths import dated_session_dir


def save_mom_file(content: str) -> str:
    """mom_reports/<2026-06-04>/mom_YYYY-MM-DD_HH-MM-SS.md"""
    now = datetime.now()
    day_folder = dated_session_dir("mom_reports", now)
    filename = now.strftime("mom_%Y-%m-%d_%H-%M-%S.md")
    file_path = os.path.join(day_folder, filename)

    with open(file_path, "w", encoding="utf-8") as f:
        f.write(content)

    return file_path
import os
from datetime import datetime

def save_mom_file(content: str):
    os.makedirs("mom_reports", exist_ok=True)

    filename = datetime.now().strftime("mom_%Y-%m-%d_%H-%M-%S.md")
    file_path = os.path.join("mom_reports", filename)

    with open(file_path, "w", encoding="utf-8") as f:
        f.write(content)

    return file_path
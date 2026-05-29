import requests

def generate_mom(transcript):
    print("Generating MOM...")

    prompt = f"""
    You are an AI assistant for daily scrum meetings.

    Convert the transcript into a structured MOM in this format:

    MOM (Daily Scrum)

    Topic:
    - Main topic discussed

    Discussion:
    - What was discussed in detail (bullet points)

    Progress:
    - What work has been done

    Blockers:
    - Any issues/blockers (if none, write "None")

    Action Items:
    - Tasks to be done next

    Next Steps:
    - What should happen next

    Keep it concise and clear.

    Transcript:
    {transcript}
    """

    response = requests.post(
        "http://localhost:11434/api/generate",
        json={
            "model": "tinyllama",
            "prompt": prompt,
            "stream": False
        }
    )

    data = response.json()

    if "response" not in data:
        print("OLLAMA ERROR:", data)
        return "MOM generation failed."

    return data["response"]
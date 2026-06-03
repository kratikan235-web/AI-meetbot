import os

from pydub import AudioSegment


class SilentAudioError(ValueError):
    pass


def to_whisper_wav(source_path: str) -> str:
    """Convert browser audio to 16 kHz mono WAV; boost quiet (non-silent) captures."""
    audio = AudioSegment.from_file(source_path)
    audio = audio.set_channels(1).set_frame_rate(16000)

    if audio.dBFS == float("-inf"):
        raise SilentAudioError(
            "Recording is completely silent. Allow microphone in Chrome and reload the extension."
        )

    if audio.dBFS < -35:
        gain_db = min(25, -18 - audio.dBFS)
        audio = audio.apply_gain(gain_db)

    wav_path = f"{os.path.splitext(source_path)[0]}.wav"
    audio.export(wav_path, format="wav")
    return wav_path


def audio_duration_seconds(wav_path: str) -> float:
    audio = AudioSegment.from_file(wav_path)
    return len(audio) / 1000.0


def is_silent_wav(wav_path: str) -> bool:
    audio = AudioSegment.from_file(wav_path)
    if audio.dBFS == float("-inf"):
        return True
    return audio.dBFS < -42

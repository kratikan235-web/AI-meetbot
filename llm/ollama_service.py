"""Backward-compatible imports — use llm.mom_service."""

from llm.mom_service import generate_mom, generate_speaker_aware_mom

__all__ = ["generate_mom", "generate_speaker_aware_mom"]

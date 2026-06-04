"""Infer speaker from transcript phrases when Meet timeline is unreliable."""

from utils.speaker_names import is_valid_person_name
from utils.text_helpers import contains_any, guess_name_from_intro, word_in_text

_HOST_OPEN = ("hello everyone", "we can start now", "can start now")
_HOST_YIELD = ("now you can continue", "you can continue", "yes, sure", "yes sure")
_THANKS = ("thank you", "thanks for your update")
_FAREWELL = ("have a nice day", "nice day")
_QUESTIONS = (
    "what are you doing",
    "give your update",
    "can you please",
    "how about you",
    "keep you happy",
    "share your update",
    "horrible hi",
    "horrible. hi",
)
_UPDATE_WORDS = (
    " class ",
    " junior ",
    " deployment ",
    " module ",
    " config ",
    " test ",
    " campaign ",
    " solar ",
    " implementation ",
    " frontline ",
    " morning ",
    " took a ",
    " multi-tenant ",
    " configuration ",
    " portfolio ",
    " vecta ",
    " django ",
)
_ASK_UPDATE = ("share your", "give your", "your update", "updates again")
_HOST_DESC = ("hosting", "initiating")


def guess_speaker_from_transcript(transcript: str) -> str | None:
    name = guess_name_from_intro(transcript)
    if name and is_valid_person_name(name):
        return name
    return None


def addressed_other_speaker(text: str, roster: list[str]) -> tuple[str | None, str | None]:
    lower = text.lower()
    for name in roster:
        if not is_valid_person_name(name):
            continue
        first = name.split()[0].lower()
        if len(first) < 3:
            continue
        others = [n for n in roster if n != name]
        if len(others) != 1:
            continue
        if contains_any(lower, (f"hello {first}", f"hi {first}", f"hey {first}")):
            return others[0], f"greeting directed at {first} (speaker is the other participant)"
        if word_in_text(first, lower) and contains_any(lower, _ASK_UPDATE):
            return others[0], f"asking {first} for an update (speaker is the other participant)"
    return None, None


def roster_names_mentioned(text_lower: str, roster: list[str], skip: set[str] | None = None) -> list[str]:
    skip = skip or set()
    mentioned = []
    for name in roster:
        if name in skip or not is_valid_person_name(name):
            continue
        first = name.split()[0].lower()
        if len(first) >= 3 and word_in_text(first, text_lower):
            mentioned.append(name)
    return mentioned


def _host_from_roster(roster: list[str], host_name: str | None) -> str | None:
    if host_name and is_valid_person_name(host_name) and host_name in roster:
        return host_name
    return None


def _first_non_host(roster: list[str], host: str | None) -> str | None:
    if host:
        others = [n for n in roster if n != host]
        return others[0] if others else None
    return roster[0] if roster else None


def content_speaker_for_segment(
    text: str,
    roster: list[str],
    prev_speaker: str | None = None,
    host_name: str | None = None,
) -> tuple[str | None, str | None]:
    lower = f" {text.lower()} "
    host = _host_from_roster(roster, host_name)

    addressed, reason = addressed_other_speaker(text, roster)
    if addressed:
        return addressed, reason

    if contains_any(
        lower,
        ("from my side", "for my side", "that's it for my side", "on my side", "my update"),
    ):
        if host_name and is_valid_person_name(host_name) and host_name in roster:
            return host_name, "recorder giving personal update"
        if len(roster) == 1:
            return roster[0], "solo update"

    if host and contains_any(lower, _HOST_OPEN):
        return host, "host opening the meeting"

    if host and contains_any(lower, _HOST_YIELD):
        return host, "host yielding or confirming turn"

    if "could i start" in lower:
        asker = _first_non_host(roster, host)
        if asker:
            return asker, "participant asking permission to start"

    if host:
        host_first = host.split()[0].lower()
        if len(host_first) >= 3 and word_in_text(host_first, lower):
            pos = lower.find(host_first)
            window = lower[pos : pos + 60]
            if contains_any(window, _HOST_DESC):
                others = [n for n in roster if n != host]
                if len(others) == 1:
                    return others[0], "describing host (speaker is not the host)"
                if len(others) >= 2:
                    commenters = roster_names_mentioned(lower, others, skip={host})
                    if commenters:
                        return commenters[0], "side comment about host"
                    return others[0], "side comment about host"

    is_thanks = contains_any(lower, _THANKS)
    is_farewell = contains_any(lower, _FAREWELL)
    is_question = contains_any(lower, _QUESTIONS)
    is_update = contains_any(lower, _UPDATE_WORDS)

    if is_thanks and not is_update:
        return host or (roster[-1] if roster else None), "thank-you / acknowledgment"
    if is_farewell and not is_thanks and not is_update and prev_speaker and len(roster) >= 2:
        other = next((n for n in roster if n != prev_speaker), None)
        if other:
            return other, f"farewell after {prev_speaker.split()[0]} spoke"
    if is_question and not is_update and "could i start" not in lower:
        return host or (roster[0] if roster else None), "facilitator question to group"
    if is_update and not is_question:
        updater = _first_non_host(roster, host) or (roster[0] if roster else None)
        if updater:
            return updater, "work-update phrasing (long answer)"

    for name in roster_names_mentioned(lower, roster):
        if host and name == host and contains_any(lower, _HOST_DESC):
            continue
        return name, f"{name.split()[0]} mentioned in text"

    return None, None

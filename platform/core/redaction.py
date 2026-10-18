"""Payment card redaction shared by the Tier 0 stores (contacts, tickets, memory).

A 13-19 digit run (spaces or hyphens allowed between digits) that passes the Luhn check is replaced with ``[REDACTED:card]``.
``platform/channels/call_transcript.py`` applies the same rule but keeps its own copy because the voice image ships that file on
its own; ``tests/test_contacts.py`` holds the two to the same result.
"""
from __future__ import annotations

import re

REDACTED_CARD = "[REDACTED:card]"
_CARD_CANDIDATE = re.compile(r"(?<![\d])(?:\d[ \-]?){12,18}\d(?![\d])")


def _luhn(digits: str) -> bool:
    total = 0
    for index, char in enumerate(reversed(digits)):
        value = int(char)
        if index % 2 == 1:
            value *= 2
            if value > 9:
                value -= 9
        total += value
    return total % 10 == 0


def redact_cards(text: str) -> str:
    """Replace Luhn-valid 13-19 digit payment card numbers."""

    def replace(match: re.Match[str]) -> str:
        digits = re.sub(r"\D", "", match.group(0))
        return REDACTED_CARD if 13 <= len(digits) <= 19 and _luhn(digits) else match.group(0)

    return _CARD_CANDIDATE.sub(replace, text)

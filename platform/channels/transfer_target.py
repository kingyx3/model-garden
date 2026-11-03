"""Strict, operator-configured LiveKit human transfer destination contract.

This is not caller-supplied routing. Reject ambiguous URI syntax at both
deployment and call time; do not let the SIP provider interpret extensions,
headers, query strings, credentials or control characters differently.
"""
from __future__ import annotations

import re

_TEL = re.compile(r"tel:\+[1-9][0-9]{1,14}", re.ASCII)
_LABEL = r"[A-Za-z0-9](?:[A-Za-z0-9-]{0,61}[A-Za-z0-9])?"
_SIP = re.compile(
    r"sip:[A-Za-z0-9_.+~-]+@(?:" + _LABEL + r")(?:\." + _LABEL + r")*"
    r"(?::(?P<port>[0-9]{1,5}))?(?:;transport=(?:tcp|udp|tls))?",
    re.ASCII,
)


def valid_transfer_target(value: object) -> bool:
    """Allow E.164 tel or narrow SIP user@host[:port][;transport=...] only."""
    if not isinstance(value, str) or len(value) > 256:
        return False
    if _TEL.fullmatch(value):
        return True
    match = _SIP.fullmatch(value)
    if match is None:
        return False
    port = match.group("port")
    return port is None or 1 <= int(port) <= 65535

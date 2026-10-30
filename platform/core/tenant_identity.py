"""Strict tenant resolution for Tier 0 operator CLIs.

Never derive an implicit shared tenant from missing or malformed compiled state.
An explicit --tenant is an operator assertion and must be authorized separately.
"""
from __future__ import annotations

import json
import pathlib
import re
from collections.abc import Mapping


def tenant_from_compiled(document: object) -> str:
    """Resolve the exact compiled tenant without rereading a mutable file.

    Autonomous workers must validate the same in-memory state they execute,
    rather than normalizing it or silently selecting a shared default.
    """
    source = document.get("source") if isinstance(document, Mapping) else None
    client = source.get("client") if isinstance(source, Mapping) else None
    if not isinstance(client, str) or not re.fullmatch(r"[a-z0-9][a-z0-9-]{0,62}", client):
        raise ValueError("tenant identity unavailable: compiled source.client must be an exact client slug")
    return client


def tenant_of(desired_state: pathlib.Path) -> str:
    try:
        document = json.loads(desired_state.read_text(encoding="utf-8"))
    except (OSError, UnicodeError, ValueError) as exc:
        raise ValueError("tenant identity unavailable: repair compiled desired state or supply an explicit trusted --tenant") from exc
    return tenant_from_compiled(document)

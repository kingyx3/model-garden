"""The Slack app manifest and Teams app package a client admin installs, generated from the gateway settings (#82, #83).

Until now the least-privilege scopes lived in prose in ``docs/channels.md``, and a client admin had to translate them into a Slack app
by hand. This renders the same settings as the artefacts the two platforms accept: a Slack app manifest (JSON) to paste into "Create
app from manifest", and a Teams app package (a zip holding ``manifest.json`` and the two required icons) to upload in the Teams admin
centre. It is pure and credential-free: it opens no network connection, creates no app and holds no secret, and nothing it emits can
hold one (the signing secret, bot token and bot credential are issued by the platforms *after* the app exists and are given to the
deployment as ``secret://`` references).

- **Only the endpoints the gateway serves.** Slack events go to ``<origin>/channels/slack/events`` and button clicks to
  ``<origin>/channels/slack/interactive``; Teams activities to ``<origin>/channels/teams/messages``. The origin is the gateway's
  ``public_origin`` and must be a bare https origin, so a manifest cannot point the platform at a plain-http or path-qualified URL.
- **Least privilege, by construction.** Slack gets ``app_mentions:read``, ``im:history`` and ``chat:write`` and subscribes to
  ``app_mention`` and ``message.im`` only; ``files:read`` and ``files:write`` are added only when ``files=True``. Token rotation, socket
  mode and org-wide deployment are off. Teams gets a bot for personal, team and group-chat scopes, ``supportsFiles`` false, and no
  resource-specific-consent or Graph permissions, and ``validDomains`` is just the gateway host.
- **Deterministic.** The same settings always produce the same bytes (fixed zip timestamps, sorted JSON), so a reviewer can diff a regenerated
  package against the one the admin installed. The icons are plain placeholders the client replaces with its own branding.
- **Not done here.** Creating the app, installing it, rotating its credentials (#68) and proving a live message round trip in a real
  workspace or tenant; file upload is not wired, so the files scopes are only for a deployment that adds it.
"""
from __future__ import annotations

import io
import json
import re
import struct
import zlib
import zipfile
from collections.abc import Mapping
from typing import Any

SLACK_BOT_SCOPES = ("app_mentions:read", "im:history", "chat:write")
SLACK_FILE_SCOPES = ("files:read", "files:write")
SLACK_BOT_EVENTS = ("app_mention", "message.im")
TEAMS_SCOPES = ("personal", "team", "groupChat")
TEAMS_SCHEMA = "https://developer.microsoft.com/json-schemas/teams/v1.16/MicrosoftTeams.schema.json"
_ORIGIN = re.compile(r"^https://(?P<host>[a-z0-9](?:[a-z0-9-]{0,61}[a-z0-9])?(?:\.[a-z0-9](?:[a-z0-9-]{0,61}[a-z0-9])?)+)(?::(?P<port>\d{1,5}))?$")
_GUID = re.compile(r"^[0-9a-f]{8}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{12}$")
_NAME = re.compile(r"^[A-Za-z0-9][A-Za-z0-9 ._'-]{0,29}$")
ZIP_TIME = (2020, 1, 1, 0, 0, 0)


class ManifestError(ValueError):
    """Raised for a setting that would produce an unsafe or unusable manifest."""


def _origin(value: Any) -> tuple[str, str]:
    match = _ORIGIN.match(value) if isinstance(value, str) else None
    if match is None or (match["port"] and not 0 < int(match["port"]) < 65536):
        raise ManifestError("public_origin must be a bare https origin such as https://coworker.client.example")
    return value, match["host"] + (f":{match['port']}" if match["port"] else "")


def _name(value: Any, label: str) -> str:
    if not isinstance(value, str) or not _NAME.match(value.strip()):
        raise ManifestError(f"{label} must be 1-30 characters of letters, digits, spaces and . _ ' -")
    return value.strip()


def _description(value: Any) -> str:
    text = " ".join(str(value).split()) if value is not None else ""
    if not text or len(text) > 130:
        raise ManifestError("description must be 1-130 characters")
    return text


def slack_manifest(public_origin: str, *, name: str, description: str = "Your AI coworker", files: bool = False) -> dict[str, Any]:
    """The Slack app manifest for a gateway at ``public_origin``. It holds no secret; Slack issues those after the app is created."""
    origin, _ = _origin(public_origin)
    label = _name(name, "name")
    scopes = list(SLACK_BOT_SCOPES) + (list(SLACK_FILE_SCOPES) if files else [])
    return {
        "display_information": {"name": label, "description": _description(description)},
        "features": {"bot_user": {"display_name": label, "always_online": False}},
        "oauth_config": {"scopes": {"bot": sorted(scopes)}},
        "settings": {
            "event_subscriptions": {"request_url": f"{origin}/channels/slack/events", "bot_events": list(SLACK_BOT_EVENTS)},
            "interactivity": {"is_enabled": True, "request_url": f"{origin}/channels/slack/interactive"},
            "org_deploy_enabled": False,
            "socket_mode_enabled": False,
            "token_rotation_enabled": False,
        },
    }


def teams_manifest(public_origin: str, *, app_id: str, name: str, description: str = "Your AI coworker",
                   package_version: str = "1.0.0", developer: str | None = None) -> dict[str, Any]:
    """The Teams app manifest for the bot ``app_id`` (its Entra application id) behind a gateway at ``public_origin``."""
    origin, host = _origin(public_origin)
    if not isinstance(app_id, str) or not _GUID.match(app_id.lower()):
        raise ManifestError("app_id must be the bot's Entra application (client) id, a GUID")
    if not isinstance(package_version, str) or not re.fullmatch(r"\d{1,4}\.\d{1,4}\.\d{1,4}", package_version):
        raise ManifestError("package_version must be like 1.0.0")
    label = _name(name, "name")
    owner = _name(developer, "developer") if developer else label
    app_id = app_id.lower()
    return {
        "$schema": TEAMS_SCHEMA,
        "manifestVersion": "1.16",
        "version": package_version,
        "id": app_id,
        "developer": {"name": owner, "websiteUrl": origin, "privacyUrl": origin, "termsOfUseUrl": origin},
        "name": {"short": label},
        "description": {"short": _description(description), "full": _description(description)},
        "icons": {"color": "color.png", "outline": "outline.png"},
        "accentColor": "#1F3A5F",
        "bots": [{
            "botId": app_id,
            "scopes": list(TEAMS_SCOPES),
            "supportsFiles": False,
            "isNotificationOnly": False,
        }],
        "validDomains": [host],
    }


def _png(size: int, rgba: tuple[int, int, int, int]) -> bytes:
    def chunk(kind: bytes, data: bytes) -> bytes:
        body = kind + data
        return struct.pack(">I", len(data)) + body + struct.pack(">I", zlib.crc32(body) & 0xFFFFFFFF)

    row = b"\x00" + bytes(rgba) * size
    return (b"\x89PNG\r\n\x1a\n" + chunk(b"IHDR", struct.pack(">IIBBBBB", size, size, 8, 6, 0, 0, 0))
            + chunk(b"IDAT", zlib.compress(row * size, 9)) + chunk(b"IEND", b""))


def teams_package(manifest: Mapping[str, Any]) -> bytes:
    """The uploadable Teams app package: ``manifest.json`` plus placeholder 192px colour and 32px outline icons, byte-for-byte reproducible."""
    if not isinstance(manifest, Mapping) or manifest.get("manifestVersion") != "1.16" or not _GUID.match(str(manifest.get("id", ""))):
        raise ManifestError("not a Teams manifest produced by teams_manifest")
    files = {
        "manifest.json": json.dumps(manifest, indent=2, sort_keys=True).encode("utf-8") + b"\n",
        "color.png": _png(192, (31, 58, 95, 255)),
        "outline.png": _png(32, (255, 255, 255, 255)),
    }
    out = io.BytesIO()
    with zipfile.ZipFile(out, "w", zipfile.ZIP_DEFLATED) as archive:
        for filename, data in files.items():
            info = zipfile.ZipInfo(filename, ZIP_TIME)
            info.compress_type = zipfile.ZIP_DEFLATED
            info.external_attr = 0o644 << 16
            archive.writestr(info, data)
    return out.getvalue()

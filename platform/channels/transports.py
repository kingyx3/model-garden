"""The network halves of the channel adapters: Slack and Teams senders, Teams token verification and the web chat outbox (#82, #83, #84).

The adapters in ``slack_channel.py``, ``teams_channel.py`` and ``web_chat.py`` compose messages and decide who is speaking, and leave
the HTTP call to an injected transport. These are those transports. Each one does exactly one thing, takes its credential from the
caller (the gateway reads it from the environment) and has an injectable HTTP function so it is tested without a network.

- **Slack** posts only to ``https://slack.com/api/`` methods with the bot token as a bearer, checks Slack's ``ok`` flag and returns the
  message ``ts``. Errors carry Slack's short error code, never the request. Uploaded files are fetched only from ``files.slack.com``
  with the bot token, bounded in size. A deliverable is posted with Slack's external upload flow: the bytes go only to Slack's file
  host and without the bot token.
- **Teams** exchanges the bot's client secret for a Bot Framework token (cached until shortly before it expires) and posts the activity to
  the conversation on the service URL the adapter remembered from a verified inbound activity. It never chooses a URL itself.
- **Teams token verification** checks a Bot Framework bearer token's RS256 signature against Microsoft's published signing keys using
  PyJWT (already pinned in the Hermes image) and returns the claims for the adapter to check issuer, audience, tenant and service URL.
- **Web chat outbox** keeps what the coworker said in each conversation, bounded and in memory, for the page to poll with its session.
"""
from __future__ import annotations

import json
import re
import threading
import time
import urllib.error
import urllib.parse
import urllib.request
from collections import OrderedDict, deque
from collections.abc import Callable, Mapping
from typing import Any

SLACK_API = "https://slack.com/api/"
SLACK_FILE_HOST = "files.slack.com"
BOT_LOGIN = "https://login.microsoftonline.com/botframework.com/oauth2/v2.0/token"
BOT_SCOPE = "https://api.botframework.com/.default"
BOT_OPENID = "https://login.botframework.com/v1/.well-known/openidconfiguration"
MAX_RESPONSE = 5 * 1024 * 1024
MAX_FILE_BYTES = 10 * 1024 * 1024
TIMEOUT = 15
Http = Callable[[str, bytes | None, Mapping[str, str], int], tuple[int, bytes]]


class TransportError(RuntimeError):
    """A send failed; carries a short code, never message text or a credential."""


def urllib_http(url: str, body: bytes | None, headers: Mapping[str, str], timeout: int) -> tuple[int, bytes]:
    request = urllib.request.Request(url, data=body, headers=dict(headers), method="POST" if body is not None else "GET")
    try:
        with urllib.request.urlopen(request, timeout=timeout) as response:  # noqa: S310 - https URLs are checked by the callers
            return response.status, response.read(MAX_RESPONSE + 1)
    except urllib.error.HTTPError as exc:
        return exc.code, b""


def _json(raw: bytes) -> Any:
    try:
        return json.loads(raw)
    except ValueError:
        return None


class SlackTransport:
    """``transport`` for ``SlackChannelAdapter`` and the sender for DM notices."""

    def __init__(self, bot_token: str, *, http: Http = urllib_http) -> None:
        if not isinstance(bot_token, str) or not bot_token.startswith("xoxb-") or len(bot_token) < 20:
            raise TransportError("the Slack bot token must be a bot token (xoxb-...)")
        self._token, self._http = bot_token, http

    def _auth(self, content_type: str | None = "application/json; charset=utf-8") -> dict[str, str]:
        return {"Authorization": f"Bearer {self._token}", **({"Content-Type": content_type} if content_type else {})}

    def _call(self, method: str, payload: Mapping[str, Any], *, form: bool = False) -> dict[str, Any]:
        if not re.fullmatch(r"[a-z]+\.[a-zA-Z.]+", method):
            raise TransportError("slack: not a Slack method")
        if form:  # the file-upload methods take form fields, not JSON
            body = urllib.parse.urlencode({key: str(value) for key, value in payload.items()}).encode("utf-8")
            status, raw = self._http(SLACK_API + method, body, self._auth("application/x-www-form-urlencoded"), TIMEOUT)
        else:
            status, raw = self._http(SLACK_API + method, json.dumps(payload).encode("utf-8"), self._auth(), TIMEOUT)
        document = _json(raw)
        if status != 200 or not isinstance(document, dict):
            raise TransportError(f"slack: HTTP {status}")
        if document.get("ok") is not True:
            raise TransportError(f"slack: {str(document.get('error') or 'error')[:40]}")
        return document

    def __call__(self, message: Mapping[str, Any]) -> str:
        method = message.get("method")
        if method != "chat.postMessage":
            raise TransportError("slack: only chat.postMessage is sent")
        document = self._call(method, {key: value for key, value in message.items() if key != "method"})
        return str(document.get("ts") or "")

    def send_text(self, address: str, text: str) -> str:
        """For ``notice_delivery``: a notice to the person's direct-message channel id."""
        return self({"method": "chat.postMessage", "channel": address, "text": text[:3500]})

    def upload_file(self, channel: str, thread: str | None, name: str, content: bytes, *, max_bytes: int = MAX_FILE_BYTES) -> str:
        """Post ``content`` into a conversation as a file (Slack's external upload flow); returns the Slack file id.

        Three calls: ask for an upload URL, send the bytes to Slack's own file host (never anywhere else, and without the bot token),
        then complete the upload into the channel and thread. Needs the app's ``files:write`` scope. Bounded in size; a failure
        carries Slack's short error code or the HTTP status, never the file.
        """
        if not isinstance(channel, str) or not re.fullmatch(r"[CDG][A-Z0-9]{2,30}", channel):
            raise TransportError("slack: not a conversation id")
        if thread is not None and not re.fullmatch(r"\d{6,12}\.\d{1,8}", str(thread)):
            raise TransportError("slack: not a thread timestamp")
        if not isinstance(content, (bytes, bytearray)) or not 0 < len(content) <= max_bytes:
            raise TransportError("slack: the file is empty or too large to upload")
        label = re.sub(r"[\x00-\x1f\x7f/\\]+", " ", str(name)).strip()[:200] or "file"
        started = self._call("files.getUploadURLExternal", {"filename": label, "length": len(content)}, form=True)
        url, file_id = started.get("upload_url"), started.get("file_id")
        parsed = urllib.parse.urlparse(url) if isinstance(url, str) else None
        if parsed is None or parsed.scheme != "https" or parsed.hostname != SLACK_FILE_HOST:
            raise TransportError("slack: the upload address is not on Slack's file host")
        if not isinstance(file_id, str) or not re.fullmatch(r"F[A-Z0-9]{2,30}", file_id):
            raise TransportError("slack: no file id")
        status, _ = self._http(url, bytes(content), {"Content-Type": "application/octet-stream"}, TIMEOUT)
        if status != 200:
            raise TransportError(f"slack: file upload HTTP {status}")
        fields: dict[str, Any] = {"files": json.dumps([{"id": file_id, "title": label}]), "channel_id": channel}
        if thread:
            fields["thread_ts"] = thread
        self._call("files.completeUploadExternal", fields, form=True)
        return file_id

    def fetch_file(self, file_id: str, *, max_bytes: int = MAX_FILE_BYTES) -> tuple[str, str, bytes]:
        """``(name, media_type, content)`` for a file shared with the coworker; only from Slack's own file host, bounded."""
        if not re.fullmatch(r"F[A-Z0-9]{2,30}", file_id or ""):
            raise TransportError("slack: not a file id")
        info = self._call("files.info", {"file": file_id})["file"]
        size = info.get("size")
        url = info.get("url_private_download") or info.get("url_private")
        if not isinstance(size, int) or size > max_bytes or not isinstance(url, str):
            raise TransportError("slack: file missing or too large")
        parsed = urllib.parse.urlparse(url)
        if parsed.scheme != "https" or parsed.hostname != SLACK_FILE_HOST:
            raise TransportError("slack: file is not on Slack's file host")
        status, content = self._http(url, None, self._auth(None), TIMEOUT)
        if status != 200 or len(content) > max_bytes:
            raise TransportError(f"slack: file download HTTP {status}")
        return str(info.get("name") or "file")[:256], str(info.get("mimetype") or "application/octet-stream")[:128], content


class TeamsTransport:
    """``transport`` for ``TeamsChannelAdapter``: posts an activity to the conversation on the verified service URL."""

    def __init__(self, app_id: str, app_secret: str, *, tenant: str = "botframework.com", http: Http = urllib_http,
                 clock: Callable[[], float] = time.time) -> None:
        if not app_id or not isinstance(app_secret, str) or len(app_secret) < 8:
            raise TransportError("teams: the bot app id and secret are required")
        self._app_id, self._secret, self._http, self._clock = app_id, app_secret, http, clock
        self._login = BOT_LOGIN.replace("botframework.com", tenant)
        self._token: tuple[str, float] | None = None
        self._lock = threading.Lock()

    def _bearer(self) -> str:
        with self._lock:
            if self._token and self._clock() < self._token[1]:
                return self._token[0]
            form = urllib.parse.urlencode({
                "grant_type": "client_credentials", "client_id": self._app_id, "client_secret": self._secret, "scope": BOT_SCOPE,
            }).encode("ascii")
            status, raw = self._http(self._login, form, {"Content-Type": "application/x-www-form-urlencoded"}, TIMEOUT)
            document = _json(raw)
            token = document.get("access_token") if isinstance(document, dict) else None
            if status != 200 or not isinstance(token, str):
                raise TransportError(f"teams: token request HTTP {status}")
            lifetime = document.get("expires_in") if isinstance(document.get("expires_in"), int) else 600
            self._token = (token, self._clock() + max(60, lifetime - 120))
            return token

    def __call__(self, message: Mapping[str, Any]) -> str:
        service_url, conversation = str(message.get("serviceUrl", "")).rstrip("/"), str(message.get("conversationId", ""))
        parsed = urllib.parse.urlparse(service_url)
        if parsed.scheme != "https" or not parsed.hostname or not conversation:
            raise TransportError("teams: no verified service URL for this conversation")
        url = f"{service_url}/v3/conversations/{urllib.parse.quote(conversation, safe='')}/activities"
        headers = {"Authorization": f"Bearer {self._bearer()}", "Content-Type": "application/json"}
        status, raw = self._http(url, json.dumps(message["activity"]).encode("utf-8"), headers, TIMEOUT)
        if status not in (200, 201, 202):
            raise TransportError(f"teams: HTTP {status}")
        document = _json(raw)
        return str(document.get("id") or "") if isinstance(document, dict) else ""


def teams_token_verifier(
    app_id: str, *, signing_key: Callable[[str], Any] | None = None, http: Http = urllib_http,
) -> Callable[[str], Mapping[str, Any]]:
    """A ``verify_token`` for ``TeamsChannelAdapter``: RS256 signature, audience and expiry; the adapter checks the rest.

    ``signing_key(token)`` returns the public key for the token's ``kid``; by default it is looked up in Microsoft's published key
    set (Bot Framework OpenID metadata), cached by PyJWT.
    """
    import jwt  # PyJWT[crypto], pinned in the Hermes image

    cache: dict[str, Any] = {}

    def default_key(token: str) -> Any:
        client = cache.get("client")
        if client is None:
            status, raw = http(BOT_OPENID, None, {}, TIMEOUT)
            document = _json(raw)
            uri = document.get("jwks_uri") if isinstance(document, dict) else None
            if status != 200 or not isinstance(uri, str) or urllib.parse.urlparse(uri).scheme != "https":
                raise TransportError("teams: signing keys unavailable")
            client = cache["client"] = jwt.PyJWKClient(uri, cache_keys=True, lifespan=3600)
        return client.get_signing_key_from_jwt(token).key

    resolve = signing_key or default_key

    def verify(token: str) -> Mapping[str, Any]:
        return jwt.decode(
            token, resolve(token), algorithms=["RS256"], audience=app_id,
            options={"require": ["exp", "iss", "aud"], "verify_iss": False},  # the adapter checks the issuer against its list
        )

    return verify


class WebOutbox:
    """What the coworker said in each web conversation, for the page to poll. Bounded and in memory.

    Messages are numbered per conversation; the page asks for everything after the last number it has seen. Conversation ids are
    prefixed with the signed-in person's own tag by the gateway, so one person cannot read another's conversation.
    """

    def __init__(self, *, per_conversation: int = 200, conversations: int = 500) -> None:
        self._max, self._limit = per_conversation, conversations
        self._items: OrderedDict[str, deque] = OrderedDict()
        self._counter: dict[str, int] = {}
        self._lock = threading.Lock()

    def push(self, payload: Mapping[str, Any]) -> str:
        conversation = str(payload["conversationId"])
        with self._lock:
            queue = self._items.setdefault(conversation, deque(maxlen=self._max))
            number = self._counter.get(conversation, 0) + 1
            self._counter[conversation] = number
            queue.append({"n": number, **{key: value for key, value in payload.items()}})
            self._items.move_to_end(conversation)
            while len(self._items) > self._limit:
                dropped, _ = self._items.popitem(last=False)
                self._counter.pop(dropped, None)
        return f"{conversation}:{number}"

    def since(self, conversation: str, after: int = 0) -> list[dict[str, Any]]:
        with self._lock:
            return [dict(item) for item in self._items.get(conversation, ()) if item["n"] > after]

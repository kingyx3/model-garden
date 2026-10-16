"""The page behind an emailed approval link: preview on GET, decide only on an explicit POST (#84).

``email_channel.ApprovalLinks`` issues a signed, expiring, single-use token bound to one ``requestId``, one decision and one
approver address. Mail scanners, link previewers and some clients fetch every link in a message, so a link that acts on a GET would
be spent, or worse approve, before the person ever reads it. This is the request handler for the page behind the link. It is a pure
function from an HTTP-shaped request to an HTTP-shaped response: it opens no socket, serves nothing, sends no mail and holds no
credential beyond the ``ApprovalLinks`` it is given. The deployment's web server, once there is one, calls ``handle``. It is **not
wired into a server or a mail provider**, and no real approval email has had live proof.

- **GET never decides.** A GET verifies the token without spending it (``ApprovalLinks.peek``) and shows what would be decided and
  who is deciding, with one button. Fetching the link any number of times changes nothing.
- **Only a POST decides.** A POST carrying the token in its body spends the single-use link and hands the resulting
  ``ApprovalResponse`` to ``on_decision``, the governed approval path, which still requires the responder to be a mapped approver.
  The token is the proof of mailbox control; nothing in the form can change the decision, the request or the approver.
- **Cross-site and framing refused.** A POST whose ``Origin`` is present and not the configured origin is refused, and every page
  sends ``X-Frame-Options: DENY``, a CSP that allows no scripts, styles, images or framing, ``no-store`` and ``no-referrer`` so the
  token does not leak through a cache or a ``Referer`` header.
- **Truthful.** The success page appears only after ``on_decision`` returned. If it raised, the link is already spent (single use is
  never undone) and the page says the decision was *not* recorded and to ask for a new link; the ledger-facing callback receives only
  the error type.
- **Escaped, bounded, no secrets.** Everything shown is HTML-escaped and bounded. The page shows the optional plain-text summary the
  deployment supplies for the request (the same redacted summary an approval prompt carries), the decision and the approver address;
  it never shows the token, arguments or a stack trace.
"""
from __future__ import annotations

import html
import importlib.util
import pathlib
import sys
from collections.abc import Callable, Mapping
from dataclasses import dataclass, field
from typing import Any
from urllib.parse import parse_qs

MAX_BODY_BYTES = 4096
MAX_TOKEN = 1500
MAX_SUMMARY = 500
Audit = Callable[[str, str, dict[str, Any]], Any]
SECURITY_HEADERS = {
    "Cache-Control": "no-store",
    "Referrer-Policy": "no-referrer",
    "X-Frame-Options": "DENY",
    "X-Content-Type-Options": "nosniff",
    "X-Robots-Tag": "noindex, nofollow",
    "Content-Security-Policy": "default-src 'none'; form-action 'self'; frame-ancestors 'none'; base-uri 'none'",
    "Content-Type": "text/html; charset=utf-8",
}


def _channel():
    name = "model_garden_text_channel"  # the shared name every adapter uses, so the error class is the same one
    module = sys.modules.get(name)
    if module is None:
        spec = importlib.util.spec_from_file_location(name, pathlib.Path(__file__).with_name("text_channel.py"))
        module = importlib.util.module_from_spec(spec)
        assert spec.loader is not None
        sys.modules[name] = module
        spec.loader.exec_module(module)
    return module


@dataclass(frozen=True)
class Response:
    status: int
    body: str
    headers: Mapping[str, str] = field(default_factory=lambda: dict(SECURITY_HEADERS))


def _page(title: str, *lines: str, form: str = "") -> str:
    body = "".join(f"<p>{html.escape(line)}</p>" for line in lines)
    return (
        "<!doctype html><html lang=\"en\"><head><meta charset=\"utf-8\"><meta name=\"robots\" content=\"noindex\">"
        f"<meta name=\"viewport\" content=\"width=device-width,initial-scale=1\"><title>{html.escape(title)}</title></head>"
        f"<body><h1>{html.escape(title)}</h1>{body}{form}</body></html>"
    )


class ApprovalConfirmation:
    """Handle the preview and confirmation requests for an emailed approval link."""

    def __init__(
        self,
        links: Any,
        on_decision: Callable[[Any], Any],
        *,
        summary_for: Callable[[str], str | None] | None = None,
        action_path: str = "/approvals/confirm",
        allowed_origin: str | None = None,
        audit: Audit | None = None,
    ) -> None:
        if not hasattr(links, "peek") or not hasattr(links, "consume"):
            raise ValueError("links must be an email_channel.ApprovalLinks")
        if not callable(on_decision):
            raise ValueError("on_decision must be the governed approval path")
        if not isinstance(action_path, str) or not action_path.startswith("/") or "?" in action_path or len(action_path) > 200:
            raise ValueError("action_path must be a server-relative path without a query")
        if allowed_origin is not None and not (isinstance(allowed_origin, str) and allowed_origin.startswith("https://") and allowed_origin.count("/") == 2):
            raise ValueError("allowed_origin must be an https origin such as https://approvals.client.example")
        self._links = links
        self._on_decision = on_decision
        self._summary_for = summary_for
        self._action = action_path
        self._origin = allowed_origin
        self._audit = audit

    def _note(self, what: str, detail: str) -> None:
        if self._audit is not None:
            try:
                self._audit("approval.link-page", "runtime:approval-links", {"outcome": what, "detail": detail[:80]})
            except Exception:  # noqa: BLE001 - the ledger must not turn a refusal into a crash
                pass

    @staticmethod
    def _refusal(error: Exception) -> Response:
        message = str(error)
        if "expired" in message:
            return Response(410, _page("This link has expired", "Ask the coworker to send a new approval request."))
        if "already used" in message:
            return Response(410, _page("This link was already used", "Each approval link works once. Nothing more was done."))
        return Response(400, _page("This link is not valid", "Open the link exactly as it was sent, or ask for a new approval request."))

    def _summary(self, request_id: str) -> str | None:
        if self._summary_for is None:
            return None
        try:
            summary = self._summary_for(request_id)
        except Exception:  # noqa: BLE001 - a lookup problem must not break the page, which still shows the reference
            return None
        return summary.strip()[:MAX_SUMMARY] if isinstance(summary, str) and summary.strip() else None

    def handle(self, method: str, *, token: str | None = None, body: bytes | None = None,
               headers: Mapping[str, str] | None = None) -> Response:
        """``GET`` takes the token from the query; ``POST`` takes it from the form body. Anything else is refused."""
        method = (method or "").upper()
        if method == "GET":
            return self._preview(token)
        if method == "POST":
            return self._confirm(body, headers or {})
        return Response(405, _page("Not allowed", "This page only shows or confirms an approval."), {**SECURITY_HEADERS, "Allow": "GET, POST"})

    def _preview(self, token: str | None) -> Response:
        if not isinstance(token, str) or not token.strip() or len(token) > MAX_TOKEN:
            return self._refusal(_channel().ChannelVerificationError("approval link is not valid"))
        try:
            request_id, approver, decision = self._links.peek(token)
        except _channel().ChannelVerificationError as exc:
            self._note("preview-refused", str(exc))
            return self._refusal(exc)
        verb = "approve" if decision == "approve" else "reject"
        summary = self._summary(request_id)
        form = (
            f"<form method=\"post\" action=\"{html.escape(self._action, quote=True)}\">"
            f"<input type=\"hidden\" name=\"token\" value=\"{html.escape(token.strip(), quote=True)}\">"
            f"<button type=\"submit\">{verb.capitalize()} this request</button></form>"
        )
        lines = [
            f"{approver} is about to {verb} request {request_id[:12]}.",
            *( [f"What it is: {summary}"] if summary else [] ),
            "Nothing happens until you press the button. Opening this page does not decide anything.",
        ]
        return Response(200, _page(f"{verb.capitalize()} this request?", *lines, form=form))

    def _confirm(self, body: bytes | None, headers: Mapping[str, str]) -> Response:
        lowered = {str(key).lower(): str(value) for key, value in headers.items()}
        origin = lowered.get("origin")
        if self._origin is not None and origin is not None and origin != self._origin:
            self._note("cross-origin", "origin mismatch")
            return Response(403, _page("Not allowed", "This confirmation must be sent from the approval page itself."))
        if not isinstance(body, (bytes, bytearray)) or not body or len(body) > MAX_BODY_BYTES:
            return self._refusal(_channel().ChannelVerificationError("approval link is not valid"))
        try:
            fields = parse_qs(bytes(body).decode("utf-8"), keep_blank_values=False, strict_parsing=True, max_num_fields=4)
        except (UnicodeDecodeError, ValueError):
            return self._refusal(_channel().ChannelVerificationError("approval link is not valid"))
        tokens = fields.get("token", [])
        if len(tokens) != 1 or not tokens[0].strip() or len(tokens[0]) > MAX_TOKEN:
            return self._refusal(_channel().ChannelVerificationError("approval link is not valid"))
        try:
            response = self._links.consume(tokens[0])
        except _channel().ChannelVerificationError as exc:
            self._note("confirm-refused", str(exc))
            return self._refusal(exc)
        try:
            self._on_decision(response)
        except Exception as exc:  # noqa: BLE001 - report only that it failed; the link is spent and stays spent
            self._note("decision-not-recorded", type(exc).__name__)
            return Response(500, _page(
                "Your decision was not recorded",
                "Something went wrong on our side, so nothing was approved or rejected.",
                "This link cannot be used again. Ask the coworker to send a new approval request.",
            ))
        return Response(200, _page("Decision recorded", f"Your decision to {response.decision} request {response.request_id[:12]} was passed to the coworker's approval record."))

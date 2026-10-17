"""A small inbound SMTP listener that feeds the built-in mail spool (#109, #84).

``mail_spool.MailSpool`` stores raw inbound mail durably and ``support_mailbox`` answers it, but nothing received mail from the
network. This is the receiving end: a deliberately minimal SMTP server (RFC 5321 subset) for a client's own support address.

- **Not a relay.** ``RCPT TO`` is accepted only for the spool's configured addresses; everything else gets ``550``. It never
  sends mail, so it cannot be used to forward spam or to bounce to a forged sender.
- **Authentication-Results are not believed.** A message's own ``Authentication-Results`` headers are removed unless the connection
  comes from a configured trusted relay (the client's real MTA that ran SPF/DKIM/DMARC and added the header). Direct senders are
  therefore never "authenticated" and stay public, read-only principals. Nothing is rewritten otherwise: the spool keeps the bytes.
- **Bounded.** One message at most ``MAX_MESSAGE_BYTES`` (the spool's limit), a line at most 4 KiB, a session at most
  ``MAX_COMMANDS`` commands, and every socket read has a timeout. A full spool answers ``452`` so the sender retries later.
- **Plain, private.** There is no TLS and no AUTH here. Put it behind the client's MTA or a TLS-terminating proxy, publish port 25 only
  there, and never expose it as an open submission service. It ledgers each accepted message through the spool and nothing else.
"""
from __future__ import annotations

import importlib.util
import pathlib
import re
import socketserver
import sys
import threading
from typing import Any

MAX_LINE = 4096
MAX_COMMANDS = 200
TIMEOUT = 30
_PATH = re.compile(r"<([^<>\s]*)>")


def _spool_module():
    name = "model_garden_smtp_intake_spool"
    module = sys.modules.get(name)
    if module is None:
        spec = importlib.util.spec_from_file_location(name, pathlib.Path(__file__).with_name("mail_spool.py"))
        module = importlib.util.module_from_spec(spec)
        assert spec.loader is not None
        sys.modules[name] = module
        spec.loader.exec_module(module)
    return module


def strip_authentication_results(raw: bytes) -> bytes:
    """Remove every ``Authentication-Results`` header (with its folded continuation lines) from the header block."""
    head, separator, body = raw.partition(b"\r\n\r\n")
    if not separator:
        head, separator, body = raw.partition(b"\n\n")
    lines, kept, skipping = head.splitlines(keepends=True), [], False
    for line in lines:
        if line[:1] in (b" ", b"\t"):
            if not skipping:
                kept.append(line)
            continue
        skipping = line.lower().startswith(b"authentication-results:")
        if not skipping:
            kept.append(line)
    return b"".join(kept) + separator + body


class _Handler(socketserver.StreamRequestHandler):
    server: "SmtpIntake"

    def _say(self, line: str) -> None:
        self.wfile.write(line.encode("ascii", "replace") + b"\r\n")
        self.wfile.flush()

    def _line(self) -> bytes | None:
        data = self.rfile.readline(MAX_LINE + 1)
        if not data or len(data) > MAX_LINE:
            return None
        return data

    def _data(self) -> bytes | None:
        """The message body up to the lone dot, dot-unstuffed; ``None`` when it ran over the size limit."""
        limit, chunks, size, over = self.server.max_bytes, [], 0, False
        while True:
            line = self._line()
            if line is None:
                raise ConnectionError
            if line in (b".\r\n", b".\n"):
                return None if over else b"".join(chunks)
            if line.startswith(b".."):
                line = line[1:]
            size += len(line)
            if size > limit:
                over = True
                chunks.clear()
            elif not over:
                chunks.append(line if line.endswith(b"\r\n") else line.rstrip(b"\n") + b"\r\n")

    def handle(self) -> None:
        self.request.settimeout(TIMEOUT)
        sender, recipients, trusted = None, [], self.client_address[0] in self.server.trusted_relays
        try:
            self._say(f"220 {self.server.hostname} ESMTP ready")
            for _ in range(MAX_COMMANDS):
                raw = self._line()
                if raw is None:
                    return
                command = raw.decode("ascii", "replace").strip()
                verb = command.split(" ", 1)[0].upper()
                if verb in ("EHLO", "HELO"):
                    sender, recipients = None, []
                    self._say(f"250-{self.server.hostname}") if verb == "EHLO" else self._say(f"250 {self.server.hostname}")
                    if verb == "EHLO":
                        self._say(f"250 SIZE {self.server.max_bytes}")
                elif verb == "MAIL":
                    match = _PATH.search(command)
                    if not command.upper().startswith("MAIL FROM:") or match is None:
                        self._say("501 syntax: MAIL FROM:<address>")
                        continue
                    sender, recipients = match.group(1), []
                    self._say("250 ok")
                elif verb == "RCPT":
                    match = _PATH.search(command)
                    if sender is None:
                        self._say("503 send MAIL FROM first")
                    elif match is None or not command.upper().startswith("RCPT TO:"):
                        self._say("501 syntax: RCPT TO:<address>")
                    elif match.group(1).strip().lower() not in self.server.spool.addresses:
                        self._say("550 no such mailbox here")
                    elif len(recipients) >= 10:
                        self._say("452 too many recipients")
                    else:
                        recipients.append(match.group(1).strip().lower())
                        self._say("250 ok")
                elif verb == "DATA":
                    if not recipients:
                        self._say("503 need a recipient first")
                        continue
                    self._say("354 end with <CRLF>.<CRLF>")
                    body = self._data()
                    if body is None:
                        self._say("552 message too large")
                    else:
                        self._say(self.server.accept(body, recipients, trusted))
                    sender, recipients = None, []
                elif verb == "RSET":
                    sender, recipients = None, []
                    self._say("250 ok")
                elif verb == "NOOP":
                    self._say("250 ok")
                elif verb == "QUIT":
                    self._say("221 bye")
                    return
                elif verb in ("VRFY", "EXPN", "AUTH", "STARTTLS"):
                    self._say("502 not supported here")
                else:
                    self._say("500 unrecognised command")
        except (OSError, ConnectionError):
            return


class SmtpIntake(socketserver.ThreadingTCPServer):
    allow_reuse_address = True
    daemon_threads = True

    def __init__(self, address: tuple[str, int], spool: Any, *, hostname: str = "mail", trusted_relays: tuple[str, ...] = ()) -> None:
        super().__init__(address, _Handler)
        self.spool, self.hostname = spool, re.sub(r"[^A-Za-z0-9.-]", "", hostname)[:100] or "mail"
        self.max_bytes = _spool_module().MAX_MESSAGE_BYTES
        self.trusted_relays = frozenset(trusted_relays)

    def accept(self, raw: bytes, recipients: list[str], trusted: bool) -> str:
        spool = _spool_module()
        if not trusted:
            raw = strip_authentication_results(raw)
        # The envelope recipient is the address mail was really delivered to; record it where the spool looks first.
        raw = b"Envelope-To: " + ", ".join(recipients).encode("ascii", "replace") + b"\r\n" + raw
        try:
            result = self.spool.ingest(raw)
        except spool.SpoolFull:
            return "452 try again later"
        except Exception:  # noqa: BLE001 - never leak why; the sender retries
            return "451 local error, try again later"
        if result["status"] in ("accepted", "duplicate"):
            return "250 queued"
        return "554 message not accepted"

    def serve_in_thread(self) -> threading.Thread:
        thread = threading.Thread(target=self.serve_forever, daemon=True)
        thread.start()
        return thread

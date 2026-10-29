"""Two-way call transcript for the voice Receptionist.

Pure Python with no LiveKit import so the privacy-critical behaviour is credential-free
testable. The voice worker feeds it caller speech (from STT) and the text actually sent
to TTS, then stores one record per call.

Guarantees:
- ordered turns with speaker (`caller` / `agent` / `human`), start offset, text and STT
  confidence where available; interrupted agent replies are stored truncated and marked;
- the record closes with a model-produced summary, an outcome and the ledger requestIds
  of governed Tool calls made during the call;
- a disclosure is configured and recorded; transcription cannot be enabled with the
  disclosure switched off unless a lawful-basis override names its owner;
- redaction (payment card numbers plus client patterns) runs before storage and is
  recorded as counts, never silent;
- storage is per client/environment, mode 0600 in a 0700 directory, bounded retention,
  retention 0 stores nothing, and a storage failure never raises into the call.

Optional environment:
- MODEL_GARDEN_TRANSCRIPT (on|off, default on)
- MODEL_GARDEN_TRANSCRIPT_PATH (default /opt/data/transcripts.jsonl)
- MODEL_GARDEN_TRANSCRIPT_RETENTION_SECONDS (default 604800 / 7 days; 0 disables storage)
- MODEL_GARDEN_TRANSCRIPT_DISCLOSURE (spoken notice; `off` disables it)
- MODEL_GARDEN_TRANSCRIPT_LAWFUL_BASIS_OVERRIDE_OWNER (required to run with disclosure off)
- MODEL_GARDEN_TRANSCRIPT_REDACT_PATTERNS (JSON array of extra regular expressions)
"""
from __future__ import annotations

import fcntl
import json
import logging
import os
import pathlib
import re
import secrets
import stat
import time
from collections.abc import Callable, Iterable, Mapping
from dataclasses import dataclass, field
from typing import Any

LOGGER = logging.getLogger("model-garden.call-transcript")

SCHEMA_VERSION = 1
SPEAKERS = ("caller", "agent", "human")
OUTCOMES = ("resolved", "booked", "message-taken", "transferred", "abandoned")
OUTCOMES_BY_STRENGTH = ("transferred", "booked", "message-taken", "resolved", "abandoned")
DEFAULT_PATH = "/opt/data/transcripts.jsonl"
DEFAULT_RETENTION_SECONDS = 7 * 24 * 60 * 60
MAX_RETENTION_SECONDS = 365 * 24 * 60 * 60
DEFAULT_DISCLOSURE = "This call is transcribed so the business can follow up."
MAX_TURNS = 2000
MAX_TURN_CHARS = 4000
MAX_SUMMARY_CHARS = 2000
MAX_REQUEST_IDS = 200
EXTRACT_CHARS = 160
MAX_PATTERNS = 20
MAX_PATTERN_CHARS = 200
CARD_REPLACEMENT = "[REDACTED:card]"
CUSTOM_REPLACEMENT = "[REDACTED:pattern]"
_CARD_CANDIDATE = re.compile(r"(?<![\d])(?:\d[ \-]?){12,18}\d(?![\d])")


class TranscriptConfigError(RuntimeError):
    """Raised at startup for an unsafe or invalid transcript configuration."""


@dataclass(frozen=True)
class TranscriptConfig:
    enabled: bool = True
    path: pathlib.Path = pathlib.Path(DEFAULT_PATH)
    retention_seconds: int = DEFAULT_RETENTION_SECONDS
    disclosure: str | None = DEFAULT_DISCLOSURE
    override_owner: str | None = None
    patterns: tuple[re.Pattern[str], ...] = field(default_factory=tuple)

    @property
    def stores(self) -> bool:
        return self.enabled and self.retention_seconds > 0


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


def redact(text: str, patterns: Iterable[re.Pattern[str]] = ()) -> tuple[str, dict[str, int]]:
    """Replace Luhn-valid card numbers and client patterns; return the text and what was hit."""
    counts = {"card": 0, "pattern": 0}

    def card(match: re.Match[str]) -> str:
        digits = re.sub(r"\D", "", match.group(0))
        if 13 <= len(digits) <= 19 and _luhn(digits):
            counts["card"] += 1
            return CARD_REPLACEMENT
        return match.group(0)

    redacted = _CARD_CANDIDATE.sub(card, text)
    for pattern in patterns:
        redacted, hits = pattern.subn(CUSTOM_REPLACEMENT, redacted)
        counts["pattern"] += hits
    return redacted, counts


def _retention(raw: str) -> int:
    try:
        value = int(raw)
    except ValueError as exc:
        raise TranscriptConfigError("MODEL_GARDEN_TRANSCRIPT_RETENTION_SECONDS must be an integer") from exc
    if value < 0 or value > MAX_RETENTION_SECONDS:
        raise TranscriptConfigError("MODEL_GARDEN_TRANSCRIPT_RETENTION_SECONDS must be between 0 and 31536000")
    return value


def _patterns(raw: str) -> tuple[re.Pattern[str], ...]:
    if not raw.strip():
        return ()
    try:
        values = json.loads(raw)
    except json.JSONDecodeError as exc:
        raise TranscriptConfigError("MODEL_GARDEN_TRANSCRIPT_REDACT_PATTERNS must be a JSON array of strings") from exc
    if not isinstance(values, list) or len(values) > MAX_PATTERNS or not all(
        isinstance(item, str) and item and len(item) <= MAX_PATTERN_CHARS for item in values
    ):
        raise TranscriptConfigError(
            f"MODEL_GARDEN_TRANSCRIPT_REDACT_PATTERNS must hold at most {MAX_PATTERNS} non-empty strings "
            f"of at most {MAX_PATTERN_CHARS} characters"
        )
    compiled = []
    for item in values:
        try:
            compiled.append(re.compile(item))
        except re.error as exc:
            raise TranscriptConfigError(f"invalid transcript redaction pattern {item!r}: {exc}") from exc
    return tuple(compiled)


def config_from_env(environ: Mapping[str, str] | None = None) -> TranscriptConfig:
    """Parse and validate configuration; an unsafe combination fails closed at startup."""
    env = os.environ if environ is None else environ
    mode = env.get("MODEL_GARDEN_TRANSCRIPT", "on").strip().lower()
    if mode not in ("on", "off"):
        raise TranscriptConfigError("MODEL_GARDEN_TRANSCRIPT must be on or off")
    retention = _retention(env.get("MODEL_GARDEN_TRANSCRIPT_RETENTION_SECONDS", str(DEFAULT_RETENTION_SECONDS)).strip())
    raw_disclosure = env.get("MODEL_GARDEN_TRANSCRIPT_DISCLOSURE")
    disclosure: str | None = DEFAULT_DISCLOSURE if raw_disclosure is None else raw_disclosure.strip()
    if disclosure in ("", "off"):
        disclosure = None
    owner = (env.get("MODEL_GARDEN_TRANSCRIPT_LAWFUL_BASIS_OVERRIDE_OWNER") or "").strip() or None
    enabled = mode == "on"
    if enabled and disclosure is None and owner is None:
        raise TranscriptConfigError(
            "transcription requires a disclosure; to run without one record a lawful basis by setting "
            "MODEL_GARDEN_TRANSCRIPT_LAWFUL_BASIS_OVERRIDE_OWNER to its accountable owner"
        )
    return TranscriptConfig(
        enabled=enabled,
        path=pathlib.Path(env.get("MODEL_GARDEN_TRANSCRIPT_PATH", DEFAULT_PATH).strip() or DEFAULT_PATH),
        retention_seconds=retention,
        disclosure=disclosure,
        override_owner=owner,
        patterns=_patterns(env.get("MODEL_GARDEN_TRANSCRIPT_REDACT_PATTERNS", "")),
    )


def _bounded(text: str, limit: int) -> tuple[str, bool]:
    return (text, False) if len(text) <= limit else (text[:limit], True)


class CallTranscript:
    """Accumulates one call. Methods never raise for transcript-content problems except
    on programmer error (an unknown speaker or outcome)."""

    def __init__(
        self,
        call_hash: str,
        agent: str,
        config: TranscriptConfig,
        *,
        started: int | None = None,
        clock: Callable[[], float] = time.monotonic,
    ) -> None:
        self.call_hash = call_hash
        self.agent = agent
        self.config = config
        self.started = int(time.time()) if started is None else started
        self._clock = clock
        self._origin = clock()
        self._turns: list[dict[str, Any]] = []
        self._redactions = {"card": 0, "pattern": 0}
        self._dropped = 0
        self._disclosure_played = False
        self._suppressed = False
        self._outcome: str | None = None
        self._transfer_offset: float | None = None

    @property
    def disclosure_text(self) -> str | None:
        """The notice to speak at the start of the call, or None when transcription is off."""
        return self.config.disclosure if self.config.enabled else None

    def mark_disclosure_played(self) -> None:
        self._disclosure_played = True

    def note_outcome(self, outcome: str) -> None:
        """Record an observed outcome; a stronger one (transferred first) is never downgraded."""
        if outcome not in OUTCOMES:
            raise ValueError(f"outcome must be one of {', '.join(OUTCOMES)}")
        noted = self._outcome
        if noted is None or OUTCOMES_BY_STRENGTH.index(outcome) < OUTCOMES_BY_STRENGTH.index(noted):
            self._outcome = outcome

    def note_transfer(self, *, offset: float | None = None) -> None:
        """Mark the moment a human transfer was requested (#115). The outcome becomes ``transferred`` and any turn the worker still hears
        afterwards is flagged ``afterTransfer``. A cold transfer takes the caller out of the room, so usually nothing follows and the
        record says so (``transfer.captured: false``) instead of looking complete."""
        if self._transfer_offset is None:
            self._transfer_offset = round(max(0.0, (self._clock() - self._origin) if offset is None else offset), 2)
        self.note_outcome("transferred")

    def _derived_outcome(self) -> str:
        """Outcome when nothing stronger was observed: a two-sided exchange counts as resolved,
        otherwise the call was abandoned. A model-produced summary may later refine this."""
        if self._outcome is not None:
            return self._outcome
        speakers = {turn["speaker"] for turn in self._turns}
        return "resolved" if {"caller", "agent"} <= speakers else "abandoned"

    def suppress(self) -> None:
        """Stop recording and discard what was gathered, for example when the disclosure could not play."""
        self._suppressed = True
        self._turns.clear()

    @property
    def suppressed(self) -> bool:
        return self._suppressed

    def add_turn(
        self,
        speaker: str,
        text: str,
        *,
        confidence: float | None = None,
        interrupted: bool = False,
        offset: float | None = None,
    ) -> None:
        """Record what was said. For the agent, pass the text actually sent to TTS."""
        if speaker not in SPEAKERS:
            raise ValueError(f"speaker must be one of {', '.join(SPEAKERS)}")
        if not self.config.enabled or self._suppressed:
            return
        text = (text or "").strip()
        if not text:
            return
        if len(self._turns) >= MAX_TURNS:
            self._dropped += 1
            return
        bounded, truncated = _bounded(text, MAX_TURN_CHARS)
        redacted, counts = redact(bounded, self.config.patterns)
        for key, value in counts.items():
            self._redactions[key] += value
        turn: dict[str, Any] = {
            "index": len(self._turns),
            "speaker": speaker,
            "offsetSeconds": round(max(0.0, (self._clock() - self._origin) if offset is None else offset), 2),
            "text": redacted,
        }
        if confidence is not None and 0.0 <= confidence <= 1.0:
            turn["confidence"] = round(confidence, 3)
        if interrupted:
            turn["interrupted"] = True
        if self._transfer_offset is not None and turn["offsetSeconds"] >= self._transfer_offset:
            turn["afterTransfer"] = True
        if truncated:
            turn["truncated"] = True
        if any(counts.values()):
            turn["redacted"] = True
        self._turns.append(turn)

    def extractive_summary(self, outcome: str | None = None) -> str:
        """A deterministic, model-free summary: what the caller first asked, the agent's last reply, turn counts and the outcome.

        It quotes the already-redacted turns (truncated), so it reveals nothing the transcript does not. It is an index into the
        call, not an understanding of it; the record marks it ``summarySource: extractive`` so nobody mistakes it for a model's.
        """
        ordered = sorted(self._turns, key=lambda turn: (turn["offsetSeconds"], turn["index"]))
        callers = [turn for turn in ordered if turn["speaker"] == "caller"]
        agents = [turn for turn in ordered if turn["speaker"] == "agent"]
        final = outcome if outcome in OUTCOMES else self._derived_outcome()
        if not ordered:
            return f"No speech was recorded. Outcome: {final}."

        def quote(turn: dict[str, Any]) -> str:
            text = " ".join(str(turn["text"]).split())
            return text if len(text) <= EXTRACT_CHARS else text[: EXTRACT_CHARS - 1].rstrip() + "…"

        parts = []
        if callers:
            parts.append(f'The caller first said: "{quote(callers[0])}"')
        if agents:
            parts.append(f'The agent\'s last reply: "{quote(agents[-1])}"')
        parts.append(f"{len(callers)} caller and {len(agents)} agent turns. Outcome: {final}.")
        if self._transfer_offset is not None:
            after = sum(1 for turn in ordered if turn.get("afterTransfer"))
            parts.append("The call was transferred to a person; " + (f"{after} later turns are recorded." if after else "what followed is not recorded."))
        return " ".join(parts)

    def build(
        self,
        *,
        outcome: str | None = None,
        summary: str | None = None,
        request_ids: Iterable[str] = (),
        ended: int | None = None,
        auto_summary: bool = False,
    ) -> dict[str, Any]:
        """Return the closed record; turns are ordered by offset, then arrival.

        ``auto_summary`` writes an extractive summary when the caller supplied none (a supplied one always wins).
        """
        if outcome is None:
            outcome = self._derived_outcome()
        if outcome not in OUTCOMES:
            raise ValueError(f"outcome must be one of {', '.join(OUTCOMES)}")
        ordered = sorted(self._turns, key=lambda turn: (turn["offsetSeconds"], turn["index"]))
        turns = [{**turn, "index": position} for position, turn in enumerate(ordered)]
        counts = dict(self._redactions)
        summary_text: str | None = None
        summary_source: str | None = None
        if not (summary and summary.strip()) and auto_summary:
            summary, summary_source = self.extractive_summary(outcome), "extractive"
        elif summary and summary.strip():
            summary_source = "provided"
        if summary and summary.strip():
            bounded, _ = _bounded(summary.strip(), MAX_SUMMARY_CHARS)
            summary_text, summary_counts = redact(bounded, self.config.patterns)
            for key, value in summary_counts.items():
                counts[key] += value
        ids = [item for item in request_ids if isinstance(item, str) and item][:MAX_REQUEST_IDS]
        disclosure: dict[str, Any] = {"configured": self.config.disclosure is not None, "played": self._disclosure_played}
        if self.config.override_owner:
            disclosure["lawfulBasisOverrideOwner"] = self.config.override_owner
        record: dict[str, Any] = {
            "type": "CallTranscript",
            "schemaVersion": SCHEMA_VERSION,
            "timestamp": self.started,
            "endedAt": int(time.time()) if ended is None else ended,
            "callHash": self.call_hash,
            "agent": self.agent,
            "disclosure": disclosure,
            "turns": turns,
            "outcome": outcome,
            "requestIds": ids,
            "redactions": counts,
        }
        if summary_text:
            record["summary"] = summary_text
            record["summarySource"] = summary_source
        if self._dropped:
            record["droppedTurns"] = self._dropped
        if self._transfer_offset is not None:
            after = sum(1 for turn in turns if turn.get("afterTransfer"))
            record["transfer"] = {"offsetSeconds": self._transfer_offset, "turnsAfter": after, "captured": after > 0}
        return record

    @property
    def reference(self) -> str:
        """Stable reference for the CRM filing step (#108/#115); never the transcript text."""
        return f"transcript:{self.call_hash}"


def _retained(handle: Any, *, now: int, retention_seconds: int) -> list[dict[str, Any]]:
    cutoff = now - retention_seconds
    handle.seek(0)
    rows: list[dict[str, Any]] = []
    for raw in handle:
        if not raw.strip():
            continue
        try:
            value = json.loads(raw)
        except json.JSONDecodeError as exc:
            raise ValueError("invalid existing voice record: malformed JSON") from exc
        # JSON booleans are Python ints; do not accept false as a timestamp.
        # Refuse any damaged row *before* truncating the original file. Silent
        # skipping destroys retained call and fallback evidence on the next write.
        if not isinstance(value, dict) or type(value.get("timestamp")) is not int:
            raise ValueError("invalid existing voice record: missing or invalid timestamp")
        if value["timestamp"] >= cutoff:
            rows.append(value)
    return rows


def _append_call_record(rows: list[dict[str, Any]], record: dict[str, Any]) -> bool:
    """One retained transcript per callHash; refuse ambiguous or conflicting evidence.

    Fallback/lifecycle records keep their normal append semantics. Existing duplicate
    transcript identities are corruption, not a reason to silently choose a winner.
    """
    is_transcript = record.get("type") == "CallTranscript"
    call_hash = record.get("callHash") if is_transcript else None
    if is_transcript and (not isinstance(call_hash, str) or not call_hash or len(call_hash) > 200):
        raise ValueError("invalid new call transcript identity")
    seen: set[str] = set()
    identical = False
    for row in rows:
        if row.get("type") != "CallTranscript":
            continue
        existing = row.get("callHash")
        if not isinstance(existing, str) or not existing or len(existing) > 200:
            raise ValueError("invalid existing call transcript identity")
        if existing in seen:
            raise ValueError("duplicate existing call transcript identity")
        seen.add(existing)
        if is_transcript and existing == call_hash:
            if row != record:
                raise ValueError("conflicting call transcript identity")
            identical = True
    return not identical


def store_private_jsonl(path: pathlib.Path, record: dict[str, Any], *, retention_seconds: int, now: int | None = None) -> None:
    """Crash-consistent private voice records, serialized by a persistent sidecar lock.

    Never truncate an existing transcript/lifecycle file in place: a crash during
    retention rewriting must leave the previous complete file available. The lock
    belongs to the *path*, not the replaced inode, so concurrent writers cannot
    lose one another's updates after atomic publication. All paths stay within the
    pinned owner-private directory and refuse symlink/hardlink redirection.
    """
    if retention_seconds < 0 or retention_seconds > MAX_RETENTION_SECONDS:
        raise ValueError("invalid voice record retention")
    path.parent.mkdir(parents=True, exist_ok=True, mode=0o700)
    directory = os.open(path.parent, os.O_RDONLY | os.O_DIRECTORY | os.O_NOFOLLOW | os.O_CLOEXEC)
    try:
        parent_stat = os.fstat(directory)
        if not stat.S_ISDIR(parent_stat.st_mode) or parent_stat.st_uid != os.geteuid():
            raise OSError("transcript directory is not owned by this worker")
        os.fchmod(directory, 0o700)

        def assert_directory() -> None:
            current = os.stat(path.parent, follow_symlinks=False)
            held = os.fstat(directory)
            if ((current.st_dev, current.st_ino) != (held.st_dev, held.st_ino)
                    or not stat.S_ISDIR(held.st_mode) or held.st_uid != os.geteuid()
                    or stat.S_IMODE(held.st_mode) != 0o700):
                raise OSError("transcript directory replaced or unsafe")

        lock_name = f".{path.name}.lock"
        assert_directory()
        lock_fd = os.open(
            lock_name, os.O_RDWR | os.O_CREAT | os.O_NOFOLLOW | os.O_NONBLOCK | os.O_CLOEXEC,
            0o600, dir_fd=directory,
        )
        try:
            lock_stat = os.fstat(lock_fd)
            if (not stat.S_ISREG(lock_stat.st_mode) or lock_stat.st_uid != os.geteuid()
                    or lock_stat.st_nlink != 1):
                raise OSError("transcript lock must be a singly linked owned regular file")
            os.fchmod(lock_fd, 0o600)
            fcntl.flock(lock_fd, fcntl.LOCK_EX)
            try:
                def assert_lock() -> None:
                    current = os.stat(lock_name, dir_fd=directory, follow_symlinks=False)
                    held = os.fstat(lock_fd)
                    if ((current.st_dev, current.st_ino) != (held.st_dev, held.st_ino)
                            or not stat.S_ISREG(held.st_mode) or held.st_uid != os.geteuid()
                            or held.st_nlink != 1 or stat.S_IMODE(held.st_mode) != 0o600):
                        raise OSError("transcript lock replaced or unsafe")

                assert_directory()
                assert_lock()
                target_fd = os.open(
                    path.name, os.O_RDWR | os.O_CREAT | os.O_NOFOLLOW | os.O_NONBLOCK | os.O_CLOEXEC,
                    0o600, dir_fd=directory,
                )
                try:
                    target_stat = os.fstat(target_fd)
                    if (not stat.S_ISREG(target_stat.st_mode) or target_stat.st_uid != os.geteuid()
                            or target_stat.st_nlink != 1):
                        raise OSError("transcript target must be a singly linked owned regular file")
                    os.fchmod(target_fd, 0o600)

                    def assert_target() -> None:
                        current = os.stat(path.name, dir_fd=directory, follow_symlinks=False)
                        held = os.fstat(target_fd)
                        if ((current.st_dev, current.st_ino) != (held.st_dev, held.st_ino)
                                or not stat.S_ISREG(held.st_mode) or held.st_uid != os.geteuid()
                                or held.st_nlink != 1 or stat.S_IMODE(held.st_mode) != 0o600):
                            raise OSError("transcript target replaced or unsafe")

                    assert_target()
                    with os.fdopen(target_fd, "r", encoding="utf-8", closefd=False) as handle:
                        rows = ([] if retention_seconds == 0 else
                                _retained(handle, now=int(time.time()) if now is None else now,
                                          retention_seconds=retention_seconds))
                    if retention_seconds > 0 and _append_call_record(rows, record):
                        rows.append(record)
                    assert_directory()
                    assert_lock()
                    assert_target()
                    temporary = f".{path.name}.{secrets.token_hex(16)}.tmp"
                    temp_fd = os.open(
                        temporary, os.O_WRONLY | os.O_CREAT | os.O_EXCL | os.O_NOFOLLOW | os.O_CLOEXEC,
                        0o600, dir_fd=directory,
                    )
                    try:
                        with os.fdopen(temp_fd, "w", encoding="utf-8", closefd=False) as handle:
                            for row in rows:
                                handle.write(json.dumps(row, sort_keys=True, ensure_ascii=False, separators=(",", ":")) + "\\n")
                            handle.flush()
                            os.fsync(temp_fd)
                        assert_directory()
                        assert_lock()
                        assert_target()
                        os.replace(temporary, path.name, src_dir_fd=directory, dst_dir_fd=directory)
                        os.fsync(directory)
                        published = os.stat(path.name, dir_fd=directory, follow_symlinks=False)
                        written = os.fstat(temp_fd)
                        if ((published.st_dev, published.st_ino) != (written.st_dev, written.st_ino)
                                or published.st_nlink != 1 or stat.S_IMODE(published.st_mode) != 0o600):
                            raise OSError("transcript publication replaced or unsafe")
                        assert_directory()
                        assert_lock()
                    finally:
                        os.close(temp_fd)
                        try:
                            os.unlink(temporary, dir_fd=directory)
                        except FileNotFoundError:
                            pass
                finally:
                    os.close(target_fd)
            finally:
                fcntl.flock(lock_fd, fcntl.LOCK_UN)
        finally:
            os.close(lock_fd)
    finally:
        os.close(directory)


def store_record(config: TranscriptConfig, record: dict[str, Any], *, now: int | None = None) -> bool:
    """Store a call transcript if enabled, with the same private file guarantees as fallback messages."""
    if not config.stores:
        return False
    store_private_jsonl(config.path, record, retention_seconds=config.retention_seconds, now=now)
    return True

def finalize(
    transcript: CallTranscript,
    *,
    outcome: str | None = None,
    summary: str | None = None,
    request_ids: Iterable[str] = (),
    on_failure: Callable[[str], None] | None = None,
    auto_summary: bool = False,
) -> dict[str, Any] | None:
    """Close and store a call's transcript without ever raising into the call.

    A storage failure is logged and reported to ``on_failure`` as an error type only (never
    transcript text) so the caller can ledger it as a failed write; the call carries on.
    """
    if not transcript.config.enabled or transcript.suppressed:
        return None
    # The worker may shut down before on_enter completes (or a speech notice may
    # never have played). A configured notice is not evidence of disclosure.
    # Never persist collected caller speech in that case. An explicitly owned
    # lawful-basis override is the only no-notice exception; deployment verifies
    # its signature separately. This is privacy suppression, not an I/O failure.
    notice = transcript.config.disclosure
    owner = transcript.config.override_owner
    if (notice and not transcript._disclosure_played) or (not notice and not (owner and owner.strip())):
        transcript.suppress()
        LOGGER.warning("call transcript suppressed: disclosure not played or no lawful-basis owner")
        return None
    try:
        record = transcript.build(outcome=outcome, summary=summary, request_ids=request_ids, auto_summary=auto_summary)
        store_record(transcript.config, record)
        return record
    except Exception as exc:  # storage or formatting must never end or block a call
        LOGGER.error("call transcript write failed: %s", type(exc).__name__)
        if on_failure is not None:
            try:
                on_failure(type(exc).__name__)
            except Exception:
                LOGGER.error("call transcript failure notification failed")
        return None

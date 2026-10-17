"""A write-only, sealed secret store for credentials an owner enters through an enrollment link (#159, #86, #129).

``secret_enrollment.EnrollmentStore`` needs a vault it can ``put`` to and ``delete`` from but never read. The deployment's other
secrets come from the protected deploy-time map; a credential a client owner types in later cannot, because nobody should have to
copy it into that map. This is the vault for it, on the governance volume the gateway and the governed sidecar already share.

- **Sealed to the sidecar.** Each value is encrypted to the sidecar's X25519 public key (an ephemeral key exchange, HKDF-SHA256,
  ChaCha20-Poly1305) with the ``secret://`` reference as associated data, so a sealed file cannot be moved to another reference.
  The gateway holds only the public key: it can store and delete, never read back. Only ``SealedResolver``, constructed from the
  private key injected into the governed sidecar alone, can open a value.
- **Stable references, versioned values.** Enrollment stores versions (``…/<connection>/v2``). Storing a version also points the
  unversioned reference (``…/<connection>``) at it, so a grant and a connection declaration name the stable reference and an owner can
  rotate the credential without restaging the connection. Deleting the version a stable reference points at removes the pointer,
  so a revoked credential stops resolving at once; it never falls back to an older version.
- **Private and bounded.** One file per reference named by its digest (no reference names on disk), 0600 in a 0700 directory, atomic
  replacement, symbolic links refused, a bounded value size. Errors name no reference and no value.
"""
from __future__ import annotations

import base64
import hashlib
import json
import os
import pathlib
import re
import secrets
import tempfile
from typing import Any

FORMAT = 1
MAX_VALUE = 4096
_INFO = b"model-garden-sealed-vault-v1"
_SECRET_REF = re.compile(r"^secret://[A-Za-z0-9][A-Za-z0-9._/-]{2,199}$")
_VERSIONED = re.compile(r"^(secret://[A-Za-z0-9][A-Za-z0-9._/-]{2,190})/v([1-9][0-9]{0,5})$")


class VaultError(RuntimeError):
    """The vault could not store, delete or open a value; the message never carries a reference or a value."""


def _crypto():
    from cryptography.hazmat.primitives import hashes, serialization
    from cryptography.hazmat.primitives.asymmetric import x25519
    from cryptography.hazmat.primitives.ciphers.aead import ChaCha20Poly1305
    from cryptography.hazmat.primitives.kdf.hkdf import HKDF

    return hashes, serialization, x25519, ChaCha20Poly1305, HKDF


def generate_keypair() -> dict[str, str]:
    """A new key pair, base64: ``public`` goes to the gateway configuration, ``private`` into the sidecar's secret map only."""
    _, serialization, x25519, _, _ = _crypto()
    private = x25519.X25519PrivateKey.generate()
    return {
        "private": base64.b64encode(private.private_bytes(serialization.Encoding.Raw, serialization.PrivateFormat.Raw, serialization.NoEncryption())).decode(),
        "public": base64.b64encode(private.public_key().public_bytes(serialization.Encoding.Raw, serialization.PublicFormat.Raw)).decode(),
    }


def public_key_of(private_key: str) -> str:
    """The base64 public key matching a base64 private key, so a deployment can prove the gateway seals to the sidecar's key."""
    _, serialization, x25519, _, _ = _crypto()
    private = x25519.X25519PrivateKey.from_private_bytes(_raw_key(private_key, "private"))
    return base64.b64encode(private.public_key().public_bytes(serialization.Encoding.Raw, serialization.PublicFormat.Raw)).decode()


def _raw_key(text: Any, label: str) -> bytes:
    try:
        raw = base64.b64decode(str(text), validate=True)
    except (ValueError, TypeError):
        raw = b""
    if len(raw) != 32:
        raise VaultError(f"the vault {label} key must be a base64 X25519 key")
    return raw


def _key(shared: bytes, ephemeral: bytes, recipient: bytes) -> bytes:
    hashes, _, _, _, HKDF = _crypto()
    return HKDF(algorithm=hashes.SHA256(), length=32, salt=ephemeral + recipient, info=_INFO).derive(shared)


def _check_ref(ref: Any) -> str:
    if not isinstance(ref, str) or not _SECRET_REF.match(ref):
        raise VaultError("not a secret:// reference")
    return ref


def _name(ref: str, suffix: str) -> str:
    return hashlib.sha256(ref.encode("utf-8")).hexdigest() + suffix


class _Files:
    def __init__(self, root: pathlib.Path) -> None:
        self.root = pathlib.Path(root)

    def _path(self, name: str) -> pathlib.Path:
        if self.root.is_symlink():
            raise VaultError("the vault directory must not be a symbolic link")
        path = self.root / name
        if path.is_symlink():
            raise VaultError("a vault entry must not be a symbolic link")
        return path

    def write(self, name: str, document: dict[str, Any]) -> None:
        self.root.mkdir(parents=True, exist_ok=True, mode=0o700)
        os.chmod(self.root, 0o700)
        target = self._path(name)
        handle, temporary = tempfile.mkstemp(prefix=".", suffix=".tmp", dir=self.root)
        try:
            with os.fdopen(handle, "w", encoding="utf-8") as stream:
                os.fchmod(stream.fileno(), 0o600)
                json.dump(document, stream, sort_keys=True)
                stream.flush()
                os.fsync(stream.fileno())
            os.replace(temporary, target)
        finally:
            if os.path.exists(temporary):
                os.unlink(temporary)

    def read(self, name: str) -> dict[str, Any] | None:
        path = self._path(name)
        if not path.is_file():
            return None
        try:
            document = json.loads(path.read_text(encoding="utf-8"))
        except (OSError, ValueError):
            raise VaultError("a vault entry is unreadable") from None
        if not isinstance(document, dict) or document.get("format") != FORMAT:
            raise VaultError("a vault entry is not in the expected format")
        return document

    def remove(self, name: str) -> None:
        self._path(name).unlink(missing_ok=True)


class SealedVault:
    """``put`` and ``delete`` by reference (the ``secret_enrollment.Vault`` protocol). Without a public key it can only delete."""

    def __init__(self, root: pathlib.Path, *, public_key: str | None = None) -> None:
        self._files = _Files(root)
        self._public = _raw_key(public_key, "public") if public_key is not None else None

    def put(self, ref: str, value: str) -> None:
        ref = _check_ref(ref)
        if self._public is None:
            raise VaultError("this vault has no public key and cannot store values")
        if not isinstance(value, str) or not value or len(value) > MAX_VALUE:
            raise VaultError(f"a value is 1 to {MAX_VALUE} characters")
        _, serialization, x25519, ChaCha20Poly1305, _ = _crypto()
        ephemeral = x25519.X25519PrivateKey.generate()
        ephemeral_public = ephemeral.public_key().public_bytes(serialization.Encoding.Raw, serialization.PublicFormat.Raw)
        shared = ephemeral.exchange(x25519.X25519PublicKey.from_public_bytes(self._public))
        nonce = secrets.token_bytes(12)
        sealed = ChaCha20Poly1305(_key(shared, ephemeral_public, self._public)).encrypt(nonce, value.encode("utf-8"), ref.encode("utf-8"))
        self._files.write(_name(ref, ".sealed"), {"format": FORMAT, "epk": base64.b64encode(ephemeral_public).decode(),
                                                   "nonce": base64.b64encode(nonce).decode(), "sealed": base64.b64encode(sealed).decode()})
        versioned = _VERSIONED.match(ref)
        if versioned:
            stable, version = versioned.group(1), int(versioned.group(2))
            current = self._files.read(_name(stable, ".current"))
            if current is None or int(current.get("version", 0)) <= version:
                self._files.write(_name(stable, ".current"), {"format": FORMAT, "version": version})

    def delete(self, ref: str) -> None:
        ref = _check_ref(ref)
        self._files.remove(_name(ref, ".sealed"))
        versioned = _VERSIONED.match(ref)
        if versioned:
            pointer = _name(versioned.group(1), ".current")
            current = self._files.read(pointer)
            if current is not None and int(current.get("version", 0)) == int(versioned.group(2)):
                self._files.remove(pointer)


class SealedResolver:
    """Opens a value by reference (exact, or the version a stable reference points at). For the governed sidecar only."""

    def __init__(self, root: pathlib.Path, *, private_key: str) -> None:
        _, serialization, x25519, _, _ = _crypto()
        self._files = _Files(root)
        self._private = x25519.X25519PrivateKey.from_private_bytes(_raw_key(private_key, "private"))
        self._public = self._private.public_key().public_bytes(serialization.Encoding.Raw, serialization.PublicFormat.Raw)

    def has(self, ref: str) -> bool:
        try:
            return self._locate(_check_ref(ref)) is not None
        except VaultError:
            return False

    def _locate(self, ref: str) -> tuple[str, dict[str, Any]] | None:
        document = self._files.read(_name(ref, ".sealed"))
        if document is not None:
            return ref, document
        pointer = self._files.read(_name(ref, ".current"))
        if pointer is None or not isinstance(pointer.get("version"), int):
            return None
        versioned = f"{ref}/v{pointer['version']}"
        document = self._files.read(_name(versioned, ".sealed"))
        return (versioned, document) if document is not None else None

    def __call__(self, ref: str) -> str:
        found = self._locate(_check_ref(ref))
        if found is None:
            raise VaultError("credential unavailable")
        actual, document = found
        _, _, x25519, ChaCha20Poly1305, _ = _crypto()
        try:
            ephemeral = base64.b64decode(document["epk"], validate=True)
            shared = self._private.exchange(x25519.X25519PublicKey.from_public_bytes(ephemeral))
            value = ChaCha20Poly1305(_key(shared, ephemeral, self._public)).decrypt(
                base64.b64decode(document["nonce"], validate=True), base64.b64decode(document["sealed"], validate=True), actual.encode("utf-8"))
            return value.decode("utf-8")
        except Exception:  # noqa: BLE001 - a wrong key, a moved file or tampering all read the same: unavailable
            raise VaultError("credential unavailable") from None

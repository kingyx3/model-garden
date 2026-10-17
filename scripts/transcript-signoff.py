#!/usr/bin/env python3
"""Sign the lawful basis for running call transcription without the spoken disclosure (#107).

``sign`` prints the ``lawful_basis_override`` block to paste under ``spec.channels.voice.transcript`` in the EnvironmentBinding. The
owner's public key must already be in ``governance.ownerKeys`` (create the pair with ``scripts/policy-signoff.py keygen``). The signature
covers the owner, the basis text and the client-environment (``--project``, the Compose project name), so it cannot be reused for another
client or edited. The deploy verifies it and refuses to start the voice worker without the disclosure otherwise. No network calls; the
private key is read from a file or ``MODEL_GARDEN_OWNER_KEY``, never from a command-line argument.
"""
from __future__ import annotations

import argparse
import importlib.util
import os
import pathlib
import sys

import yaml

ROOT = pathlib.Path(__file__).resolve().parents[1]
_spec = importlib.util.spec_from_file_location("model_garden_transcript_signoff_lawful_basis", ROOT / "platform" / "core" / "lawful_basis.py")
LAWFUL = importlib.util.module_from_spec(_spec)
sys.modules[_spec.name] = LAWFUL
_spec.loader.exec_module(LAWFUL)  # type: ignore[union-attr]


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--owner", required=True, help="the owner identity listed in governance.ownerKeys")
    parser.add_argument("--basis", required=True, help="the lawful basis, in the owner's words (at most 300 characters)")
    parser.add_argument("--project", required=True, help="the client-environment slug used as the Compose project name")
    parser.add_argument("--key-file", type=pathlib.Path, help="file holding the base64 private key (or set MODEL_GARDEN_OWNER_KEY)")
    args = parser.parse_args(argv)
    private = args.key_file.read_text(encoding="utf-8").strip() if args.key_file else os.environ.get("MODEL_GARDEN_OWNER_KEY", "")
    try:
        block = LAWFUL.sign_override(args.owner, args.basis, args.project, private)
    except ValueError as exc:
        print(str(exc), file=sys.stderr)
        return 1
    print(yaml.safe_dump({"lawful_basis_override": block}, sort_keys=False))
    return 0


if __name__ == "__main__":
    sys.exit(main())

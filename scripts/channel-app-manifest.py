#!/usr/bin/env python3
"""Write the Slack app manifest or Teams app package for a gateway, to install in the client's workspace or tenant (#82, #83).

``slack`` prints the manifest JSON; ``teams`` writes the uploadable ``.zip``. Both are generated from the gateway's public origin and hold
no secret and make no network call: the signing secret, bot token and bot credential are issued by the platform after the app is created
and go to the deployment as ``secret://`` references. See ``docs/channels.md``.
"""
from __future__ import annotations

import argparse
import importlib.util
import json
import pathlib
import sys

ROOT = pathlib.Path(__file__).resolve().parents[1]
_spec = importlib.util.spec_from_file_location("model_garden_app_manifests_cli", ROOT / "platform" / "channels" / "app_manifests.py")
MANIFESTS = importlib.util.module_from_spec(_spec)
sys.modules[_spec.name] = MANIFESTS
_spec.loader.exec_module(MANIFESTS)  # type: ignore[union-attr]


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    commands = parser.add_subparsers(dest="command", required=True)
    for kind in ("slack", "teams"):
        sub = commands.add_parser(kind)
        sub.add_argument("--origin", required=True, help="the gateway public_origin, e.g. https://coworker.client.example")
        sub.add_argument("--name", required=True, help="the app's display name (at most 30 characters)")
        sub.add_argument("--description", default="Your AI coworker")
        if kind == "slack":
            sub.add_argument("--files", action="store_true", help="also request files:read and files:write")
        else:
            sub.add_argument("--app-id", required=True, help="the bot's Entra application (client) id")
            sub.add_argument("--package-version", default="1.0.0")
            sub.add_argument("--output", type=pathlib.Path, required=True, help="where to write the .zip package")
    args = parser.parse_args(argv)
    try:
        if args.command == "slack":
            print(json.dumps(MANIFESTS.slack_manifest(args.origin, name=args.name, description=args.description, files=args.files), indent=2))
        else:
            manifest = MANIFESTS.teams_manifest(args.origin, app_id=args.app_id, name=args.name, description=args.description,
                                                package_version=args.package_version)
            args.output.write_bytes(MANIFESTS.teams_package(manifest))
            print(f"wrote {args.output}")
    except MANIFESTS.ManifestError as exc:
        print(str(exc), file=sys.stderr)
        return 1
    return 0


if __name__ == "__main__":
    sys.exit(main())

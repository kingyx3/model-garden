#!/usr/bin/env python3
"""Parse-test an uploaded business-rules file before it becomes a Tier 0 KnowledgeSource.

  knowledge-check.py rules.md                      # sections, digest and warnings
  knowledge-check.py rules.txt --query "gas smell"  # also show what a search would cite
  knowledge-check.py rules.md --classification confidential  # also warn who it could not answer

Exit 0 when the file is usable (warnings are advisory), 1 when it cannot be used. Searching here
ignores approval status: it only shows what the file contains, not what the coworker may use.
"""
from __future__ import annotations

import argparse
import importlib.util
import json
import pathlib
import sys

ROOT = pathlib.Path(__file__).resolve().parents[1]


def _load():
    path = ROOT / "platform" / "core" / "knowledge.py"
    spec = importlib.util.spec_from_file_location("model_garden_knowledge_check", path)
    module = importlib.util.module_from_spec(spec)
    assert spec.loader is not None
    spec.loader.exec_module(module)
    return module


KNOWLEDGE = _load()


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    parser.add_argument("file", type=pathlib.Path)
    parser.add_argument("--query", help="show the sections a search for this text would cite")
    parser.add_argument("--classification", choices=KNOWLEDGE.CLASSIFICATIONS, help="the classification you plan to give the source")
    args = parser.parse_args(argv)
    try:
        text = KNOWLEDGE.read_text_file(args.file)
        report = KNOWLEDGE.check_source(text, filename=args.file.name, classification=args.classification)
        if args.query:
            index = KNOWLEDGE.build_index(args.file.stem, text, filename=args.file.name, spec={"status": "approved"})
            found = KNOWLEDGE.search(index, args.query)
            report["searchPreview"] = [
                {"citation": item["citation"], "title": item["title"], "score": item["score"]} for item in found["documents"]
            ]
    except (KNOWLEDGE.KnowledgeError, OSError) as exc:
        print(f"Knowledge file rejected: {exc}", file=sys.stderr)
        return 1
    print(json.dumps(report, indent=2, sort_keys=True))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())

#!/usr/bin/env bash
set -euo pipefail

ROOT="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
LOCK="$ROOT/platform/hermes.lock"
PROFILE_DIR="${1:-/tmp/model-garden-hermes-smoke}"
VENV_DIR="${HERMES_SMOKE_VENV:-/tmp/model-garden-hermes-venv}"
SOURCE_DIR="${HERMES_SMOKE_SOURCE:-/tmp/model-garden-hermes-source}"
DESIRED_STATE="${HERMES_SMOKE_DESIRED_STATE:-/tmp/model-garden-receptionist.json}"

repo="$(awk -F= '$1=="repository" {print $2}' "$LOCK")"
commit="$(awk -F= '$1=="commit" {print $2}' "$LOCK")"
version="$(awk -F= '$1=="version" {print $2}' "$LOCK")"

if [[ -z "$repo" || -z "$commit" || -z "$version" ]]; then
  echo "Invalid Hermes lockfile: $LOCK" >&2
  exit 1
fi

# The bundled receptionist demonstrates an optional future booking Skill. The retired
# native Calendar cannot be provisioned until a reviewed upstream route is active.
SMOKE_WORKSPACE="$(mktemp -d)"
trap 'rm -rf "$SMOKE_WORKSPACE"' EXIT
cp -R "$ROOT/examples/workspace/." "$SMOKE_WORKSPACE/"
python3 - "$SMOKE_WORKSPACE/agents/front-office/receptionist/agent.yaml" <<'PYDISABLE'
from pathlib import Path
import sys
import yaml
file = Path(sys.argv[1])
agent = yaml.safe_load(file.read_text())
agent["spec"]["disabledSkills"] = ["book-appointment"]
file.write_text(yaml.safe_dump(agent, sort_keys=False))
PYDISABLE
python3 "$ROOT/scripts/compile-workspace.py" "$SMOKE_WORKSPACE" --agent receptionist --output "$DESIRED_STATE"
rm -rf "$PROFILE_DIR"
python3 "$ROOT/scripts/provision-hermes-profile.py" "$DESIRED_STATE" "$PROFILE_DIR"

rm -rf "$SOURCE_DIR" "$VENV_DIR"
git clone --quiet --filter=blob:none "$repo" "$SOURCE_DIR"
git -C "$SOURCE_DIR" checkout --quiet "$commit"
python3 -m venv "$VENV_DIR"
"$VENV_DIR/bin/python" -m pip install --disable-pip-version-check --quiet --upgrade pip
"$VENV_DIR/bin/python" -m pip install --disable-pip-version-check --quiet -e "$SOURCE_DIR"

actual_version="$(HERMES_HOME="$PROFILE_DIR" "$VENV_DIR/bin/python" -c 'import importlib.metadata; print(importlib.metadata.version("hermes-agent"))')"
if [[ "$actual_version" != "$version" ]]; then
  echo "Pinned Hermes version mismatch: expected $version, got $actual_version" >&2
  exit 1
fi

HERMES_HOME="$PROFILE_DIR" "$VENV_DIR/bin/hermes" skills list > /tmp/model-garden-hermes-skills.txt
for skill in answer-faq take-message human-handoff; do
  grep -q "$skill" /tmp/model-garden-hermes-skills.txt || {
    echo "Hermes did not discover Model Garden Skill: $skill" >&2
    cat /tmp/model-garden-hermes-skills.txt >&2
    exit 1
  }
done

HERMES_HOME="$PROFILE_DIR" "$VENV_DIR/bin/python" - <<'PY'
from pathlib import Path
import os
import yaml

from agent.prompt_builder import load_soul_md
from hermes_constants import get_hermes_home, get_skills_dir
from agent.skill_utils import parse_frontmatter

home = Path(os.environ["HERMES_HOME"]).resolve()
assert get_hermes_home().resolve() == home
soul = load_soul_md()
assert soul and "Receptionist" in soul

config = yaml.safe_load((home / "config.yaml").read_text())
assert config["model"]["provider"] == "openai"
assert config["model"]["default"] == "approved-openai-model"

skills_dir = get_skills_dir().resolve()
assert skills_dir == (home / "skills").resolve()
expected = {"answer-faq", "take-message", "human-handoff", "log-conversation"}
seen = set()
for skill_file in (skills_dir / "model-garden").glob("*/SKILL.md"):
    frontmatter, body = parse_frontmatter(skill_file.read_text())
    name = frontmatter.get("name")
    if name in expected:
        assert body.strip()
        seen.add(name)
assert seen == expected, (seen, expected)
assert not (home / ".env").exists()
print("Hermes loaded the Model Garden Receptionist identity and all managed Skills")
PY

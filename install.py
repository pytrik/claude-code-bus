#!/usr/bin/env python3
"""Installer for the claude-code-bus cutover. Run it yourself:

    python C:/repos/claude-code-bus/install.py            # preview
    python C:/repos/claude-code-bus/install.py --force    # do it
    python C:/repos/claude-code-bus/install.py --uninstall --force

It does two things, both idempotent:

1. Copies skill/SKILL.md to ~/.claude/skills/claude-code-bus/ so sessions
   auto-discover the bus.
2. Registers `python <repo>/ccbus.py guard` as a Stop hook in
   ~/.claude/settings.json (backup written beside it first).

It does NOT touch the old ccbus skill or its stop-guard hook entry; remove
those yourself once every session using the old bus has wound down --
running both guards side by side is harmless (each watches its own bus).
Preview mode prints exactly what would change and writes nothing.
"""

from __future__ import annotations

import argparse
import json
import shutil
import sys
import time
from pathlib import Path

REPO = Path(__file__).resolve().parent
CLAUDE_DIR = Path.home() / ".claude"
SKILL_SRC = REPO / "skill"
SKILL_DST = CLAUDE_DIR / "skills" / "claude-code-bus"
SETTINGS = CLAUDE_DIR / "settings.json"
HOOK_COMMAND = f"python {(REPO / 'ccbus.py').as_posix()} guard"


def load_settings() -> dict:
    if not SETTINGS.exists():
        return {}
    text = SETTINGS.read_text(encoding="utf-8-sig")
    settings = json.loads(text)  # a parse error must abort, never overwrite
    if not isinstance(settings, dict):
        raise SystemExit(f"{SETTINGS} is not a JSON object; refusing to touch it")
    return settings


def stop_entries(settings: dict) -> list:
    return settings.setdefault("hooks", {}).setdefault("Stop", [])


def hook_installed(settings: dict) -> bool:
    for entry in stop_entries(settings):
        for hook in entry.get("hooks", []):
            if hook.get("command") == HOOK_COMMAND:
                return True
    return False


def install(force: bool) -> None:
    actions = []
    if SKILL_DST.exists():
        actions.append(f"update skill files in {SKILL_DST}")
    else:
        actions.append(f"create {SKILL_DST} with the skill files")
    settings = load_settings()
    if hook_installed(settings):
        actions.append("Stop hook already registered; leave settings.json alone")
        register = False
    else:
        actions.append(f"append Stop hook to {SETTINGS}: {HOOK_COMMAND}")
        register = True

    if not force:
        print("install would:")
        for a in actions:
            print(f"  - {a}")
        print("re-run with --force to apply")
        return

    SKILL_DST.mkdir(parents=True, exist_ok=True)
    for f in SKILL_SRC.iterdir():
        shutil.copy2(f, SKILL_DST / f.name)
    print(f"installed skill files to {SKILL_DST}")

    if register:
        backup = SETTINGS.with_name(
            f"settings.json.bak-{time.strftime('%Y%m%d%H%M%S')}")
        if SETTINGS.exists():
            shutil.copy2(SETTINGS, backup)
            print(f"backed up settings to {backup}")
        stop_entries(settings).append(
            {"hooks": [{"type": "command", "command": HOOK_COMMAND}]})
        SETTINGS.write_text(json.dumps(settings, indent=2) + "\n",
                            encoding="utf-8")
        print(f"registered Stop hook in {SETTINGS}")
    print("done. The old ccbus skill and its hook were not touched; remove "
          "them when the old bus is retired.")


def uninstall(force: bool) -> None:
    actions = []
    if SKILL_DST.exists():
        actions.append(f"remove {SKILL_DST}")
    settings = load_settings()
    if hook_installed(settings):
        actions.append(f"remove Stop hook from {SETTINGS}")
    if not actions:
        print("nothing installed; nothing to do")
        return
    if not force:
        print("uninstall would:")
        for a in actions:
            print(f"  - {a}")
        print("re-run with --force to apply")
        return

    if SKILL_DST.exists():
        shutil.rmtree(SKILL_DST)
        print(f"removed {SKILL_DST}")
    if hook_installed(settings):
        backup = SETTINGS.with_name(
            f"settings.json.bak-{time.strftime('%Y%m%d%H%M%S')}")
        shutil.copy2(SETTINGS, backup)
        print(f"backed up settings to {backup}")
        entries = stop_entries(settings)
        for entry in entries:
            entry["hooks"] = [h for h in entry.get("hooks", [])
                              if h.get("command") != HOOK_COMMAND]
        settings["hooks"]["Stop"] = [e for e in entries if e.get("hooks")]
        SETTINGS.write_text(json.dumps(settings, indent=2) + "\n",
                            encoding="utf-8")
        print(f"removed Stop hook from {SETTINGS}")
    print("done. The bus database (~/.claude-code-bus) was left in place.")


def main() -> int:
    p = argparse.ArgumentParser(description=__doc__.split("\n")[0])
    p.add_argument("--force", action="store_true",
                   help="apply; without it, only preview")
    p.add_argument("--uninstall", action="store_true")
    args = p.parse_args()
    if args.uninstall:
        uninstall(args.force)
    else:
        install(args.force)
    return 0


if __name__ == "__main__":
    sys.exit(main())

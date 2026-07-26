#!/usr/bin/env python3

from __future__ import annotations

import ast
from pathlib import Path


ROOT = Path(__file__).resolve().parents[1]


def main() -> int:
    handoff = (ROOT / "docs/CODEX_HANDOFF.md").read_text(encoding="utf-8")
    script = ROOT / "scripts/build_workspace_handoff.py"
    ast.parse(script.read_text(encoding="utf-8"))

    required_handoff_terms = (
        "resources/integration-manifest.json",
        "resources/selected-presets.json",
        "glic-metal.git.bundle",
        "stb.git.bundle",
        "test-materials.tar.zst",
        "gallery-evidence.tar.zst",
        "codec-toolchains-cache.tar.zst",
        "agent-conversation-summary.md",
        "28",
        "53 adopted",
        "25 adopted offline",
        "960×540",
        "20fps",
    )
    for term in required_handoff_terms:
        assert term in handoff, term

    script_text = script.read_text(encoding="utf-8")
    assert "glic-metal-workspace-handoff-v1" in script_text
    assert "--include-large-data" in script_text
    assert "build_macos_sdk.sh" in script_text
    assert "inspect_glic_metal_sdk.py" in script_text
    assert "raw_agent_sessions" in script_text
    print("PASS workspace handoff contract")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())

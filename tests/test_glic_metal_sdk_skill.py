#!/usr/bin/env python3

import json
import subprocess
import sys
import tempfile
from pathlib import Path


ROOT = Path(__file__).resolve().parents[1]
SKILL = ROOT / "skills/glic-metal-sdk-integration"
INSPECTOR = SKILL / "scripts/inspect_glic_metal_sdk.py"


def run_inspector(root: Path) -> subprocess.CompletedProcess[str]:
    return subprocess.run(
        [
            sys.executable,
            str(INSPECTOR),
            str(root),
            "--lane",
            "all",
            "--strict",
            "--json",
        ],
        check=False,
        capture_output=True,
        text=True,
    )


def main() -> int:
    skill_text = (SKILL / "SKILL.md").read_text(encoding="utf-8")
    metadata_text = (SKILL / "agents/openai.yaml").read_text(encoding="utf-8")
    assert "name: glic-metal-sdk-integration" in skill_text
    assert "$glic-metal-sdk-integration" in metadata_text

    source_result = run_inspector(ROOT)
    assert source_result.returncode == 0, source_result.stderr
    source_report = json.loads(source_result.stdout)
    assert source_report["ok"] is True
    assert source_report["layout"] == "source"
    assert source_report["library"]["cmake_target"] == "GlicMetal::GlicMetal"
    assert source_report["selected_preset_bank"]["count"] == 19
    assert source_report["selected_preset_bank"]["category_counts"] == {
        "original": 14,
        "spatial": 4,
        "codec": 1,
    }
    assert all(source_report["headers"].values())

    with tempfile.TemporaryDirectory() as empty:
        missing_result = run_inspector(Path(empty))
    assert missing_result.returncode == 1
    missing_report = json.loads(missing_result.stdout)
    assert missing_report["ok"] is False
    assert "integration-manifest.json" in missing_report["errors"][0]

    print("PASS GLIC Metal SDK integration skill")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())

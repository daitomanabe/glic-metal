#!/usr/bin/env python3

from __future__ import annotations

import json
from pathlib import Path
import subprocess
import sys


ROOT = Path(__file__).resolve().parents[1]


def main() -> int:
    subprocess.run(
        [
            sys.executable,
            str(ROOT / "scripts/import_gallery_curation.py"),
            "--check",
        ],
        cwd=ROOT,
        check=True,
    )
    review = json.loads(
        (ROOT / "resources/glic-metal-gallery-review.json").read_text()
    )
    adopted = json.loads(
        (ROOT / "resources/glic-metal-adopted-presets.json").read_text()
    )
    selected = json.loads(
        (ROOT / "resources/selected-presets.json").read_text()
    )
    assert review["counts"] == {
        "adopted": 53,
        "rejected": 41,
        "pending": 347,
    }
    assert len(review["items"]) == 441
    assert len(adopted["items"]) == 53
    assert selected["realtime_only"] is True
    assert selected["count"] == 28
    assert selected["category_counts"] == {
        "original": 14,
        "spatial": 8,
        "codec": 6,
    }
    assert all(
        item["category"] in {"original", "spatial", "codec"}
        and item["realtime_certified"] is True
        for item in selected["mixed_glitch_patterns"]
    )
    print("PASS canonical gallery curation and realtime SDK bank")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())

from __future__ import annotations

from collections import deque
import json
from pathlib import Path
import sys

import numpy as np


ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "scripts"))

import process_codec_lab as lab  # noqa: E402
import evolutionary_codec_search as search  # noqa: E402


def main() -> int:
    assert len(lab.SYNTAX_EFFECTS) == 12
    assert len(lab.ANALYSIS_EFFECTS) == 5
    assert len(lab.EFFECTS) == 17
    catalog = json.loads(
        (ROOT / "resources" / "codec-lab-effects.json").read_text()
    )
    assert catalog["schema"] == "glic-codec-lab-effect-catalog-v1"
    assert catalog["realtime_crossbreed"]["effect_names"] == [
        "dual_codec_crossbreed",
        "codec_pingpong",
        "gop_accordion",
        "bframe_braid",
        "plane_split_codec",
        "roi_quality_islands",
        "codec_phase_mosaic",
        "encoder_hot_swap",
        "pts_rubberband",
        "bitrate_raster",
    ]
    assert catalog["syntax_lab"]["effect_names"] == list(lab.SYNTAX_EFFECTS)
    assert (
        set(catalog["analysis_and_search"]["effect_names"])
        == set(lab.ANALYSIS_EFFECTS) | {"evolutionary_codec_search"}
    )

    height, width = 48, 64
    x = np.arange(width, dtype=np.uint8)[None, :]
    y = np.arange(height, dtype=np.uint8)[:, None]
    base = np.empty((height, width, 3), dtype=np.uint8)
    base[..., 0] = x
    base[..., 1] = y
    base[..., 2] = x // 2 + y // 2
    history = deque(
        [np.roll(base, shift, axis=1) for shift in range(8)],
        maxlen=12,
    )
    frozen = None
    for index, effect in enumerate(lab.EFFECTS):
        output, frozen = lab.transform_frame(
            effect,
            np.roll(base, index + 1, axis=0),
            history,
            np.flip(base, axis=1),
            frozen,
            index + 10,
            0.72,
            0.56,
            0.68,
            1234,
            0.74,
        )
        assert output.shape == base.shape, effect
        assert output.dtype == np.uint8, effect

    generated = [search.candidate(index, 1234) for index in range(34)]
    assert len({entry["name"] for entry in generated}) == 34
    assert set(lab.EFFECTS).issubset({entry["effect"] for entry in generated})
    print("PASS codec lab effects, catalog, and evolutionary recipes")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())

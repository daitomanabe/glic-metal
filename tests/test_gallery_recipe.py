#!/usr/bin/env python3

from __future__ import annotations

from pathlib import Path
import sys


ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "scripts"))

import build_glitch_algorithm_gallery as gallery  # noqa: E402
import run_gallery_recipe as recipe_runner  # noqa: E402


def rejected(call) -> bool:
    try:
        call()
    except ValueError:
        return True
    return False


def main() -> int:
    catalog = gallery.read_json(gallery.DEFAULT_CATALOG)
    assert len(recipe_runner.gallery_keys(catalog)) == 441
    assert len(recipe_runner.gallery_keys(catalog, "spatial")) == 42

    task = recipe_runner.task_from_key(catalog, "original:vv01::saturation")
    assert task.preset["parameters"]["strength"] == 1.592

    recipe = {
        "schema": gallery.RECIPE_SCHEMA,
        "algorithm_id": "original:vv01",
        "variant_id": "structure",
        "family": "spatial",
        "effect": "ignored",
        "parameters": {
            **task.preset["parameters"],
            "strength": 0.25,
        },
    }
    from_recipe = recipe_runner.task_from_recipe(catalog, recipe)
    assert from_recipe.algorithm["family"] == "original"
    assert from_recipe.algorithm["effect"] == "vv01"
    assert from_recipe.preset["parameters"]["strength"] == 0.25

    assert rejected(
        lambda: recipe_runner.task_from_key(catalog, "original:vv01")
    )
    assert rejected(
        lambda: recipe_runner.task_from_key(catalog, "original:none::rhythm")
    )
    assert rejected(
        lambda: recipe_runner.task_from_recipe(
            catalog, {**recipe, "schema": "other"}
        )
    )
    assert rejected(
        lambda: recipe_runner.task_from_recipe(
            catalog,
            {**recipe, "parameters": {"strength": 1.0}},
        )
    )
    assert rejected(
        lambda: recipe_runner.task_from_recipe(
            catalog,
            {
                **recipe,
                "parameters": {**recipe["parameters"], "strength": "1.0"},
            },
        )
    )

    vvc = next(
        gallery.RenderTask(row, row["presets"][0])
        for row in catalog["algorithms"]
        if row["family"] == "generation" and row["codec"] == "vvc"
    )
    assert gallery.processing_fps(vvc.algorithm, 30) == 12
    assert gallery.processing_fps(task.algorithm, 30) == 30
    dependencies = {
        name: name
        for name in ("ffedit", "avmenc", "avmdec", "vvencapp")
    }
    paths = [Path(name) for name in ("in", "donor", "raw", "report", "stage")]
    command = gallery.processor_command(
        vvc,
        *paths,
        dependencies,
        width=960,
        height=540,
        fps=30,
        frame_limit=48,
    )
    assert command[command.index("--width") + 1] == "960"
    assert command[command.index("--fps") + 1] == "12"
    assert command[command.index("--max-frames") + 1] == "48"
    default = gallery.processor_command(vvc, *paths, dependencies)
    assert default[default.index("--width") + 1] == str(gallery.WIDTH)
    assert default[default.index("--max-frames") + 1] == "60"

    assert gallery.wet_mix_filter(0.62, hold_seconds=5) == (
        "[0:v]scale=480:270:flags=lanczos,fps=24,"
        "tpad=stop_mode=clone:stop_duration=5,trim=duration=5,"
        "setpts=PTS-STARTPTS[src];"
        "[1:v]scale=480:270:flags=lanczos,fps=24,"
        "tpad=stop_mode=clone:stop_duration=5,trim=duration=5,"
        "setpts=PTS-STARTPTS[effect];"
        "[src][effect]blend=all_expr='A*0.38+B*0.62':shortest=1,"
        "format=yuv420p[out]"
    )
    assert "tpad" not in gallery.wet_mix_filter(0.82, width=960, height=540)
    print("PASS gallery recipe: keys, validation, and processor options")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())

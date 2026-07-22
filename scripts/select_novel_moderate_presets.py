#!/usr/bin/env python3
"""Select moderately complex, visually novel realtime preset recipes.

This is a second-stage selector for ``rank_search_results.py`` output. It
keeps only the middle of the observed pattern-complexity distribution, then
uses max-min selection against both a prior ranking and the current selection.
The resulting JSON is a directly runnable canonical-recipe bank.
"""

from __future__ import annotations

import argparse
import csv
import hashlib
import html
import json
import math
import os
import shutil
import tempfile
from collections import Counter
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Iterable

from rank_search_results import perceptual_distance


SCHEMA = "glic-novel-moderate-preset-selection-v1"
ADOPTED_SCHEMA = "glic-adopted-preset-selection-v1"
POLICY = "middle-complexity-reference-novelty-maxmin-v1"
COMPLEXITY_FIELDS = (
    "edge_density",
    "local_contrast",
    "shape_entropy",
    "log_area_cv",
    "log_blobs",
    "residual_edge_mean",
    "residual_scale_entropy",
)
COMPLEXITY_WEIGHTS = {
    "edge_density": 0.22,
    "local_contrast": 0.18,
    "shape_entropy": 0.18,
    "log_area_cv": 0.12,
    "log_blobs": 0.10,
    "residual_edge_mean": 0.12,
    "residual_scale_entropy": 0.08,
}
EMBEDDING_APPEARANCE_VECTOR_LENGTHS = {
    "luma_grid": 64,
    "edge_grid": 64,
    "color_grid": 48,
}
EMBEDDING_RESIDUAL_VECTOR_LENGTHS = {
    "residual_luma_grid": 64,
    "residual_edge_grid": 64,
    "residual_scale_histogram": 7,
    "residual_orientation": 2,
}


def finite(value: Any, default: float = 0.0) -> float:
    if isinstance(value, bool):
        return default
    try:
        number = float(value)
    except (TypeError, ValueError):
        return default
    return number if math.isfinite(number) else default


def clamp(value: float, low: float = 0.0, high: float = 1.0) -> float:
    return min(high, max(low, value))


def quantile(values: Iterable[float], fraction: float) -> float:
    ordered = sorted(float(value) for value in values if math.isfinite(float(value)))
    if not ordered:
        return 0.0
    position = clamp(fraction) * (len(ordered) - 1)
    lower = int(math.floor(position))
    upper = int(math.ceil(position))
    if lower == upper:
        return ordered[lower]
    weight = position - lower
    return ordered[lower] * (1.0 - weight) + ordered[upper] * weight


def robust_unit(value: float, low: float, high: float) -> float:
    if high - low <= 1e-12:
        return 0.5
    return clamp((value - low) / (high - low))


def atomic_write_text(path: Path, text: str) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    descriptor, temporary_name = tempfile.mkstemp(
        prefix=f".{path.name}.", suffix=".tmp", dir=path.parent
    )
    temporary = Path(temporary_name)
    try:
        with os.fdopen(descriptor, "w", encoding="utf-8", newline="") as handle:
            handle.write(text)
            handle.flush()
            os.fsync(handle.fileno())
        os.replace(temporary, path)
    finally:
        temporary.unlink(missing_ok=True)


def load_ranking(path: Path) -> dict[str, Any]:
    payload = json.loads(path.read_text(encoding="utf-8"))
    candidates = payload.get("candidates")
    if not isinstance(candidates, list):
        raise ValueError(f"ranking has no candidate list: {path}")
    return payload


def morphology_available(item: dict[str, Any]) -> bool:
    if item.get("morphology_available") is True:
        return True
    perceptual = item.get("perceptual")
    return isinstance(perceptual, dict) and perceptual.get("residual_reference") == "dry_wet"


def distance_ready(item: dict[str, Any]) -> dict[str, Any]:
    item["morphology_available"] = morphology_available(item)
    return item


def eligible_candidates(payload: dict[str, Any]) -> list[dict[str, Any]]:
    result = []
    for raw in payload["candidates"]:
        if not isinstance(raw, dict) or raw.get("eligible") is not True:
            continue
        certification = raw.get("performance_certification")
        if isinstance(certification, dict) and certification.get("certified") is not True:
            continue
        if not isinstance(raw.get("perceptual"), dict) or not isinstance(raw.get("liveliness"), dict):
            continue
        result.append(distance_ready(dict(raw)))
    return result


def normalized_entropy(values: Any) -> float:
    if not isinstance(values, list) or len(values) < 2:
        return 0.0
    positive = [max(0.0, finite(value)) for value in values]
    total = sum(positive)
    if total <= 1e-12:
        return 0.0
    probabilities = [value / total for value in positive if value > 0.0]
    return -sum(value * math.log(value) for value in probabilities) / math.log(len(positive))


def raw_complexity(item: dict[str, Any]) -> dict[str, float]:
    perceptual = item["perceptual"]
    liveliness = item["liveliness"]
    residual_edges = perceptual.get("residual_edge_grid")
    edge_values = (
        [finite(value) for value in residual_edges]
        if isinstance(residual_edges, list) and residual_edges
        else [0.0]
    )
    return {
        "edge_density": finite(perceptual.get("edge_density")),
        "local_contrast": finite(perceptual.get("local_contrast")),
        "shape_entropy": finite(liveliness.get("shape_entropy")),
        "log_area_cv": math.log1p(max(0.0, finite(liveliness.get("area_cv")))),
        "log_blobs": math.log1p(max(0.0, finite(liveliness.get("blobs")))),
        "residual_edge_mean": sum(edge_values) / len(edge_values),
        "residual_scale_entropy": normalized_entropy(
            perceptual.get("residual_scale_histogram")
        ),
    }


def calibrate_complexity(candidates: list[dict[str, Any]]) -> dict[str, dict[str, float]]:
    raw_rows = [raw_complexity(item) for item in candidates]
    calibration: dict[str, dict[str, float]] = {}
    for field in COMPLEXITY_FIELDS:
        values = [row[field] for row in raw_rows]
        calibration[field] = {
            "q10": quantile(values, 0.10),
            "q50": quantile(values, 0.50),
            "q90": quantile(values, 0.90),
        }
    return calibration


def attach_complexity(
    candidates: list[dict[str, Any]], calibration: dict[str, dict[str, float]]
) -> dict[str, float]:
    scores = []
    for item in candidates:
        raw = raw_complexity(item)
        components = {
            field: robust_unit(raw[field], calibration[field]["q10"], calibration[field]["q90"])
            for field in COMPLEXITY_FIELDS
        }
        score = sum(components[name] * COMPLEXITY_WEIGHTS[name] for name in COMPLEXITY_FIELDS)
        item["_complexity_raw"] = raw
        item["_complexity_components"] = components
        item["_complexity_score"] = score
        scores.append(score)
    bands = {
        "q20": quantile(scores, 0.20),
        "q50": quantile(scores, 0.50),
        "q80": quantile(scores, 0.80),
    }
    half_span = max(bands["q50"] - bands["q20"], bands["q80"] - bands["q50"], 0.05)
    for item in candidates:
        item["_moderate_fit"] = clamp(
            1.0 - abs(item["_complexity_score"] - bands["q50"]) / (half_span * 1.5)
        )
    return bands


def reference_novelty(
    candidate: dict[str, Any], references: list[dict[str, Any]]
) -> tuple[float, float, str]:
    if not references:
        fallback = finite(candidate.get("knn_novelty"), 0.5)
        return fallback, fallback, ""
    distances = sorted(
        (perceptual_distance(candidate, reference), str(reference.get("recipe_hash") or ""))
        for reference in references
    )
    nearest_distance, nearest_hash = distances[0]
    count = min(5, len(distances))
    mean_five = sum(distance for distance, _ in distances[:count]) / count
    return nearest_distance, mean_five, nearest_hash


def attach_novelty(
    candidates: list[dict[str, Any]], references: list[dict[str, Any]]
) -> dict[str, float]:
    nearest_values = []
    for item in candidates:
        nearest, mean_five, nearest_hash = reference_novelty(item, references)
        item["_nearest_reference_distance"] = nearest
        item["_mean_five_reference_distance"] = mean_five
        item["_nearest_reference_hash"] = nearest_hash
        nearest_values.append(nearest)
    bands = {
        "q20": quantile(nearest_values, 0.20),
        "q50": quantile(nearest_values, 0.50),
        "q90": quantile(nearest_values, 0.90),
    }
    quality_values = [finite(item.get("core_utility"), 0.5) for item in candidates]
    quality_low = quantile(quality_values, 0.10)
    quality_high = quantile(quality_values, 0.90)
    for item in candidates:
        item["_reference_novelty_score"] = robust_unit(
            item["_nearest_reference_distance"], bands["q20"], bands["q90"]
        )
        item["_quality_score"] = robust_unit(
            finite(item.get("core_utility"), 0.5), quality_low, quality_high
        )
        item["_base_score"] = (
            0.45 * item["_moderate_fit"]
            + 0.35 * item["_reference_novelty_score"]
            + 0.20 * item["_quality_score"]
        )
    return bands


def mechanism(item: dict[str, Any]) -> str:
    return str(item.get("mechanism_family") or item.get("recipe_family") or "unknown")


def select_maxmin(candidates: list[dict[str, Any]], count: int) -> list[dict[str, Any]]:
    remaining = list(candidates)
    selected: list[dict[str, Any]] = []
    family_counts: Counter[str] = Counter()
    coverage_target = min(count, len({mechanism(item) for item in remaining}))
    while remaining and len(selected) < count:
        require_new_family = len(selected) < coverage_target
        options: list[tuple[float, float, dict[str, Any]]] = []
        for item in remaining:
            family = mechanism(item)
            if require_new_family and family_counts[family] > 0:
                continue
            if family_counts[family] >= 2:
                continue
            nearest_selected = (
                1.0
                if not selected
                else min(perceptual_distance(item, other) for other in selected)
            )
            family_bonus = 1.0 if family_counts[family] == 0 else 0.0
            gain = 0.48 * item["_base_score"] + 0.42 * nearest_selected + 0.10 * family_bonus
            options.append((gain, nearest_selected, item))
        if not options and require_new_family:
            coverage_target = len(selected)
            continue
        if not options:
            break
        options.sort(
            key=lambda row: (
                -row[0],
                -row[1],
                -row[2]["_nearest_reference_distance"],
                str(row[2].get("recipe_hash") or ""),
            )
        )
        gain, nearest_selected, chosen = options[0]
        chosen["_selection_gain"] = gain
        chosen["_nearest_selected_distance"] = nearest_selected
        selected.append(chosen)
        family_counts[mechanism(chosen)] += 1
        remaining.remove(chosen)
    return selected


def resolve_preview(run_dir: Path, item: dict[str, Any]) -> Path:
    raw = item.get("preview_path")
    if not isinstance(raw, str) or not raw:
        raise ValueError(f"candidate has no preview path: {item.get('recipe_hash')}")
    path = Path(raw).expanduser()
    resolved = path.resolve() if path.is_absolute() else (run_dir / path).resolve()
    try:
        resolved.relative_to(run_dir.resolve())
    except ValueError as error:
        raise ValueError(f"preview escapes run directory: {raw}") from error
    if not resolved.is_file():
        raise FileNotFoundError(f"candidate preview is missing: {resolved}")
    return resolved


def public_item(item: dict[str, Any], index: int, preview_name: str) -> dict[str, Any]:
    return {
        "selection_rank": index,
        "name": f"novel_mod_{index:02d}_{mechanism(item)}_{item.get('recipe_hash')}",
        "candidate_id": item.get("candidate_id"),
        "recipe_hash": item.get("recipe_hash"),
        "canonical": item.get("canonical"),
        "recipe": item.get("recipe"),
        "ready_to_run_args": item.get("ready_to_run_args"),
        "preview": preview_name,
        "mechanism_family": mechanism(item),
        "artifact_scale_bucket": item.get("artifact_scale_bucket"),
        "artifact_orientation": item.get("artifact_orientation"),
        "complexity_score": item["_complexity_score"],
        "moderate_fit": item["_moderate_fit"],
        "complexity_components": item["_complexity_components"],
        "nearest_prior_distance": item["_nearest_reference_distance"],
        "mean_five_prior_distance": item["_mean_five_reference_distance"],
        "nearest_prior_recipe_hash": item["_nearest_reference_hash"],
        "nearest_selected_distance": item["_nearest_selected_distance"],
        "core_utility": item.get("core_utility"),
        "selection_gain": item["_selection_gain"],
        "performance_certification": item.get("performance_certification"),
    }


def write_csv(path: Path, items: list[dict[str, Any]]) -> None:
    fields = (
        "selection_rank", "name", "recipe_hash", "mechanism_family",
        "artifact_scale_bucket", "complexity_score", "moderate_fit",
        "nearest_prior_distance", "nearest_selected_distance", "core_utility",
        "preview", "canonical",
    )
    with path.open("w", encoding="utf-8", newline="") as handle:
        writer = csv.DictWriter(handle, fieldnames=fields, extrasaction="ignore")
        writer.writeheader()
        writer.writerows(items)


def write_contact_sheet(path: Path, items: list[dict[str, Any]], output_dir: Path) -> None:
    from PIL import Image, ImageDraw, ImageFont

    columns = 3
    tile_width, image_height, caption_height = 480, 270, 62
    rows = max(1, math.ceil(len(items) / columns))
    canvas = Image.new("RGB", (columns * tile_width, rows * (image_height + caption_height)), "#050505")
    draw = ImageDraw.Draw(canvas)
    font = ImageFont.load_default()
    for index, item in enumerate(items):
        column, row = index % columns, index // columns
        left, top = column * tile_width, row * (image_height + caption_height)
        image = Image.open(output_dir / item["preview"]).convert("RGB")
        image.thumbnail((tile_width, image_height), Image.Resampling.LANCZOS)
        frame = Image.new("RGB", (tile_width, image_height), "black")
        frame.paste(image, ((tile_width - image.width) // 2, (image_height - image.height) // 2))
        canvas.paste(frame, (left, top))
        draw.text((left + 10, top + image_height + 8), item["name"], fill="#f2f2f2", font=font)
        draw.text(
            (left + 10, top + image_height + 28),
            f"complex {item['complexity_score']:.3f}  prior {item['nearest_prior_distance']:.3f}",
            fill="#9b9b9b", font=font,
        )
    canvas.save(path, optimize=True)


def selection_set_id(items: list[dict[str, Any]]) -> str:
    identity = "\n".join(str(item.get("recipe_hash") or "") for item in items)
    return hashlib.sha256(identity.encode("utf-8")).hexdigest()[:16]


def json_for_script(value: Any) -> str:
    """Encode JSON without allowing data to terminate the script element."""
    return (
        json.dumps(value, ensure_ascii=False, separators=(",", ":"), allow_nan=False)
        .replace("&", "\\u0026")
        .replace("<", "\\u003c")
        .replace(">", "\\u003e")
        .replace("\u2028", "\\u2028")
        .replace("\u2029", "\\u2029")
    )


def build_html(items: list[dict[str, Any]], summary: dict[str, Any]) -> str:
    cards = []
    for item in items:
        canonical = html.escape(str(item.get("canonical") or ""))
        name = html.escape(str(item["name"]))
        recipe_hash = html.escape(str(item["recipe_hash"]), quote=True)
        media = f'<img src="{html.escape(item["preview"])}" alt="{html.escape(item["name"])}">'
        if item.get("video"):
            media = (
                f'<video controls muted loop playsinline preload="metadata" '
                f'poster="{html.escape(item["preview"])}">'
                f'<source src="{html.escape(item["video"])}" type="video/mp4"></video>'
            )
        cards.append(
            f'''<article class="preset-card" data-preset-key="{recipe_hash}"><div class="media-wrap">{media}<label class="decision"><input class="preset-checkbox" type="checkbox" value="{recipe_hash}" aria-label="{name}を採用"><span><span class="checkmark">✓</span>採用する</span></label></div><div class="card-copy"><small>#{item["selection_rank"]} · {html.escape(item["mechanism_family"])}</small><h2>{name}</h2><p>complexity <b>{item["complexity_score"]:.3f}</b> · prior distance <b>{item["nearest_prior_distance"]:.3f}</b> · selected distance <b>{item["nearest_selected_distance"]:.3f}</b></p><details><summary>canonical recipe</summary><code>{canonical}</code></details></div></article>'''
        )
    set_id = selection_set_id(items)
    browser_data = {
        "schema": SCHEMA,
        "adopted_schema": ADOPTED_SCHEMA,
        "policy": POLICY,
        "selection_set_id": set_id,
        "presets": items,
    }
    document = '''<!doctype html>
<html lang="ja">
<head>
<meta charset="utf-8">
<meta name="viewport" content="width=device-width,initial-scale=1">
<title>GLIC preset review</title>
<style>
:root{color-scheme:dark;font-family:-apple-system,BlinkMacSystemFont,"Segoe UI",sans-serif;--accent:#7ee29a;--panel:#111;--line:#333}
*{box-sizing:border-box}
body{margin:0;background:#050505;color:#eee}
.hero{padding:24px;border-bottom:1px solid #292929}
.hero h1{margin:0 0 8px;font-size:clamp(22px,4vw,36px)}
.hero p,p{color:#aaa}
.review-bar{position:sticky;top:0;z-index:20;display:flex;flex-wrap:wrap;align-items:center;justify-content:space-between;gap:10px 16px;padding:14px 18px;background:rgba(8,8,8,.94);border-bottom:1px solid #292929;backdrop-filter:blur(12px)}
.review-count{min-width:150px;font-size:15px}
.review-count strong{color:var(--accent);font-size:24px}
.review-actions{display:flex;align-items:center;justify-content:flex-end;gap:8px;flex-wrap:wrap}
button{appearance:none;border:1px solid #444;border-radius:7px;padding:9px 12px;background:#1b1b1b;color:#eee;font:inherit;cursor:pointer}
button:hover:not(:disabled){border-color:#777;background:#242424}
button.primary{border-color:#4e9d67;background:#183823}
button.primary:hover:not(:disabled){background:#214b30}
button[aria-pressed="true"]{border-color:var(--accent);color:var(--accent)}
button:disabled{cursor:not-allowed;opacity:.38}
#selection-status{flex-basis:100%;margin:0;color:#888;font-size:12px;text-align:right}
main{display:grid;grid-template-columns:repeat(auto-fill,minmax(360px,1fr));gap:16px;padding:18px}
article{background:var(--panel);border:2px solid var(--line);border-radius:10px;overflow:hidden;transition:border-color .16s,box-shadow .16s,opacity .16s}
article.adopted{border-color:var(--accent);box-shadow:0 0 0 1px rgba(126,226,154,.22),0 10px 32px rgba(0,0,0,.35)}
article.filtered-out{display:none}
.media-wrap{position:relative;padding:0}
img,video{display:block;width:100%;aspect-ratio:16/9;object-fit:cover;background:#000}
.decision{position:absolute;top:12px;right:12px;display:block;cursor:pointer;user-select:none}
.decision input{position:absolute;opacity:0;pointer-events:none}
.decision span{display:flex;align-items:center;gap:7px;padding:9px 12px;border:1px solid #666;border-radius:999px;background:rgba(10,10,10,.86);color:#eee;font-weight:700;box-shadow:0 2px 12px rgba(0,0,0,.35)}
.decision input:focus-visible+span{outline:2px solid white;outline-offset:2px}
.decision input:checked+span{border-color:var(--accent);background:#1b4a2a;color:#fff}
.checkmark{display:grid!important;width:19px;height:19px;padding:0!important;place-items:center;border:1px solid #777!important;border-radius:5px!important;background:#111!important;color:transparent!important;font-size:13px}
.decision input:checked+span .checkmark{border-color:var(--accent)!important;background:var(--accent)!important;color:#07150b!important}
.card-copy{padding:14px}
small{color:var(--accent);font:12px ui-monospace,monospace}
h2{font:600 14px ui-monospace,monospace;overflow-wrap:anywhere}
code{display:block;margin-top:10px;color:#bbb;overflow-wrap:anywhere}
@media(max-width:680px){.review-bar{align-items:flex-start;flex-direction:column}.review-actions{justify-content:flex-start}#selection-status{text-align:left}main{grid-template-columns:1fr;padding:12px}}
</style>
</head>
<body>
<header class="hero">
  <h1>新規preset 採用レビュー</h1>
  <p>映像を確認し、使いたい候補の「採用する」をチェックしてください。初回は全て未選択です。</p>
  <p>__AVAILABLE__ candidates / __MODERATE__ moderate / __ELIGIBLE__ realtime-certified · policy __POLICY__</p>
</header>
<section class="review-bar" aria-label="採用操作">
  <div class="review-count"><strong id="adopted-count">0</strong> / __AVAILABLE__ 採用</div>
  <div class="review-actions">
    <button id="show-adopted" type="button" aria-pressed="false" disabled>採用だけ表示</button>
    <button id="clear-selection" type="button" disabled>選択解除</button>
    <button id="export-csv" type="button" disabled>CSVを保存</button>
    <button id="export-json" class="primary" type="button" disabled>採用JSONを保存</button>
  </div>
  <p id="selection-status" role="status">チェックした候補だけが採用ファイルに入ります。選択状態はこのブラウザに保存されます。</p>
</section>
<main>__CARDS__</main>
<script id="preset-data" type="application/json">__BROWSER_DATA__</script>
<script>
(() => {
  "use strict";
  const data = JSON.parse(document.getElementById("preset-data").textContent);
  const presets = data.presets;
  const knownHashes = new Set(presets.map((preset) => String(preset.recipe_hash)));
  const storageKey = `glic:adopted-presets:${data.selection_set_id}`;
  const selected = new Set();
  const boxes = [...document.querySelectorAll(".preset-checkbox")];
  const cards = [...document.querySelectorAll(".preset-card")];
  const count = document.getElementById("adopted-count");
  const showAdopted = document.getElementById("show-adopted");
  const clearSelection = document.getElementById("clear-selection");
  const exportJson = document.getElementById("export-json");
  const exportCsv = document.getElementById("export-csv");
  const status = document.getElementById("selection-status");
  let filterAdopted = false;

  try {
    const saved = JSON.parse(localStorage.getItem(storageKey) || "[]");
    if (Array.isArray(saved)) {
      saved.filter((hash) => knownHashes.has(String(hash))).forEach((hash) => selected.add(String(hash)));
    }
  } catch (_) {
    status.textContent = "ブラウザ保存は利用できませんが、この画面内での選択と書き出しは利用できます。";
  }

  function saveState() {
    try {
      localStorage.setItem(storageKey, JSON.stringify([...selected]));
    } catch (_) {
      // Selection and downloads remain functional without local storage.
    }
  }

  function sync() {
    boxes.forEach((box) => {
      box.checked = selected.has(box.value);
    });
    cards.forEach((card) => {
      const adopted = selected.has(card.dataset.presetKey);
      card.classList.toggle("adopted", adopted);
      card.classList.toggle("filtered-out", filterAdopted && !adopted);
    });
    const adoptedCount = selected.size;
    count.textContent = String(adoptedCount);
    [clearSelection, exportJson, exportCsv, showAdopted].forEach((button) => {
      button.disabled = adoptedCount === 0;
    });
    showAdopted.setAttribute("aria-pressed", String(filterAdopted));
    showAdopted.textContent = filterAdopted ? "全候補を表示" : "採用だけ表示";
    saveState();
  }

  boxes.forEach((box) => {
    box.addEventListener("change", () => {
      if (box.checked) selected.add(box.value);
      else selected.delete(box.value);
      if (selected.size === 0) filterAdopted = false;
      status.textContent = `${selected.size}件を採用候補に設定しました。`;
      sync();
    });
  });

  showAdopted.addEventListener("click", () => {
    filterAdopted = !filterAdopted;
    sync();
  });

  clearSelection.addEventListener("click", () => {
    selected.clear();
    filterAdopted = false;
    status.textContent = "選択を解除しました。";
    sync();
  });

  function adoptedPresets() {
    return presets.filter((preset) => selected.has(String(preset.recipe_hash)));
  }

  function adoptedPayload() {
    const adopted = adoptedPresets();
    return {
      schema: data.adopted_schema,
      generated_at: new Date().toISOString(),
      source: {
        schema: data.schema,
        policy: data.policy,
        selection_set_id: data.selection_set_id,
        selection_origin: "checked_only"
      },
      summary: {
        available: presets.length,
        adopted: adopted.length,
        mechanism_families: [...new Set(adopted.map((preset) => preset.mechanism_family))].sort()
      },
      presets: adopted
    };
  }

  function download(name, type, body) {
    const link = document.createElement("a");
    const url = URL.createObjectURL(new Blob([body], {type}));
    link.href = url;
    link.download = name;
    document.body.appendChild(link);
    link.click();
    link.remove();
    setTimeout(() => URL.revokeObjectURL(url), 1000);
  }

  function csvCell(value) {
    const text = value === null || value === undefined
      ? ""
      : (typeof value === "object" ? JSON.stringify(value) : String(value));
    return `"${text.replaceAll('"', '""')}"`;
  }

  exportJson.addEventListener("click", () => {
    download("adopted-presets.json", "application/json;charset=utf-8", JSON.stringify(adoptedPayload(), null, 2) + "\\n");
    status.textContent = `${selected.size}件だけを adopted-presets.json に保存しました。`;
  });

  exportCsv.addEventListener("click", () => {
    const fields = ["selection_rank", "name", "recipe_hash", "mechanism_family", "canonical", "ready_to_run_args"];
    const rows = adoptedPresets().map((preset) => fields.map((field) => csvCell(preset[field])).join(","));
    download("adopted-presets.csv", "text/csv;charset=utf-8", "\ufeff" + fields.join(",") + "\\n" + rows.join("\\n") + "\\n");
    status.textContent = `${selected.size}件だけを adopted-presets.csv に保存しました。`;
  });

  sync();
})();
</script>
</body>
</html>
'''
    return (
        document.replace("__AVAILABLE__", str(summary["selected"]))
        .replace("__MODERATE__", str(summary["moderate_pool"]))
        .replace("__ELIGIBLE__", str(summary["eligible"]))
        .replace("__POLICY__", html.escape(POLICY))
        .replace("__CARDS__", "".join(cards))
        .replace("__BROWSER_DATA__", json_for_script(browser_data))
    )


def numeric_features(item: dict[str, Any], include_residual: bool) -> list[float]:
    perceptual = item["perceptual"]
    liveliness = item["liveliness"]
    raw = item.get("raw_metrics") if isinstance(item.get("raw_metrics"), dict) else {}
    values = [
        finite(perceptual.get("edge_density")), finite(perceptual.get("local_contrast")),
        finite(perceptual.get("colorfulness")), finite(perceptual.get("saturation_mean")),
        finite(perceptual.get("channel_separation")), finite(liveliness.get("shape_entropy")),
        math.log1p(max(0.0, finite(liveliness.get("area_cv")))),
        math.log1p(max(0.0, finite(liveliness.get("blobs")))),
        finite(raw.get("changed_ratio")), finite(raw.get("luma_correlation")),
    ]
    vector_lengths = dict(EMBEDDING_APPEARANCE_VECTOR_LENGTHS)
    if include_residual:
        values.extend(
            [
                finite(perceptual.get("residual_mask_coverage")),
                finite(perceptual.get("dominant_artifact_scale_fraction")),
            ]
        )
        vector_lengths.update(EMBEDDING_RESIDUAL_VECTOR_LENGTHS)
    for name, length in vector_lengths.items():
        vector = perceptual.get(name)
        if not isinstance(vector, list) or len(vector) != length:
            values.extend([0.0] * length)
        else:
            values.extend(finite(value) for value in vector)
    return values


def write_embedding_features(
    path: Path,
    candidates: list[dict[str, Any]],
    references: list[dict[str, Any]],
    selected_hashes: set[str],
) -> int:
    rows = []
    expected_length = None
    all_items = [*references, *candidates]
    include_residual = bool(all_items) and all(
        morphology_available(item) for item in all_items
    )
    for cohort, source in (("prior", references), ("new", candidates)):
        for item in source:
            features = numeric_features(item, include_residual)
            if expected_length is None:
                expected_length = len(features)
            if len(features) != expected_length:
                continue
            recipe_hash = str(item.get("recipe_hash") or "")
            group = "new_selected" if cohort == "new" and recipe_hash in selected_hashes else cohort
            rows.append((recipe_hash, group, mechanism(item), features))
    feature_count = expected_length or 0
    with path.open("w", encoding="utf-8", newline="") as handle:
        writer = csv.writer(handle)
        writer.writerow(["recipe_hash", "source_group", "mechanism", *[f"feature_{i:03d}" for i in range(feature_count)]])
        for recipe_hash, group, family, features in rows:
            writer.writerow([recipe_hash, group, family, *features])
    return len(rows)


def parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("run_dir", type=Path)
    parser.add_argument("--ranking", type=Path)
    parser.add_argument("--reference-ranking", type=Path)
    parser.add_argument("--output-dir", type=Path)
    parser.add_argument("--count", type=int, default=12)
    return parser.parse_args()


def main() -> int:
    args = parse_args()
    run_dir = args.run_dir.expanduser().resolve()
    ranking_path = (args.ranking or run_dir / "ranking.json").expanduser().resolve()
    output_dir = (args.output_dir or run_dir / "novel-moderate-selection").expanduser().resolve()
    if args.count <= 0:
        raise SystemExit("--count must be positive")
    ranking = load_ranking(ranking_path)
    candidates = eligible_candidates(ranking)
    if not candidates:
        raise SystemExit("ranking has no eligible candidates")
    references: list[dict[str, Any]] = []
    reference_path = None
    if args.reference_ranking is not None:
        reference_path = args.reference_ranking.expanduser().resolve()
        references = eligible_candidates(load_ranking(reference_path))

    calibration = calibrate_complexity(candidates)
    complexity_bands = attach_complexity(candidates, calibration)
    novelty_bands = attach_novelty(candidates, references)
    moderate = [
        item for item in candidates
        if complexity_bands["q20"] <= item["_complexity_score"] <= complexity_bands["q80"]
        and item["_nearest_reference_distance"] >= novelty_bands["q20"]
        and finite(item["perceptual"].get("residual_mask_coverage")) >= 0.05
    ]
    if len(moderate) < args.count:
        moderate = sorted(
            candidates,
            key=lambda item: (-item["_moderate_fit"], -item["_reference_novelty_score"]),
        )[: max(args.count, len(candidates) // 2)]
    selected = select_maxmin(moderate, min(args.count, len(moderate)))
    if not selected:
        raise SystemExit("moderate-complexity selection produced no candidates")

    output_dir.mkdir(parents=True, exist_ok=True)
    (output_dir / "previews").mkdir(parents=True, exist_ok=True)
    public_items = []
    for index, item in enumerate(selected, 1):
        source = resolve_preview(run_dir, item)
        preview_name = f"previews/{index:02d}_{mechanism(item)}_{item['recipe_hash']}.png"
        shutil.copy2(source, output_dir / preview_name)
        exported = public_item(item, index, preview_name)
        video_name = f"videos/{index:02d}_{mechanism(item)}_{item['recipe_hash']}.mp4"
        if (output_dir / video_name).is_file():
            exported["video"] = video_name
        public_items.append(exported)

    summary = {
        "eligible": len(candidates),
        "moderate_pool": len(moderate),
        "selected": len(public_items),
        "reference_candidates": len(references),
        "unique_mechanisms": len({item["mechanism_family"] for item in public_items}),
        "minimum_pairwise_distance": min(
            (item["nearest_selected_distance"] for item in public_items[1:]), default=None
        ),
        "minimum_prior_distance": min(item["nearest_prior_distance"] for item in public_items),
    }
    summary["embedding_rows"] = write_embedding_features(
        output_dir / "embedding-features.csv", candidates, references,
        {str(item.get("recipe_hash")) for item in selected},
    )
    payload = {
        "schema": SCHEMA,
        "policy": POLICY,
        "generated_at": datetime.now(timezone.utc).isoformat().replace("+00:00", "Z"),
        "source": {
            "run_dir": str(run_dir),
            "ranking": str(ranking_path),
            "reference_ranking": "" if reference_path is None else str(reference_path),
        },
        "criteria": {
            "complexity_calibration": calibration,
            "complexity_middle_band": complexity_bands,
            "reference_novelty_band": novelty_bands,
            "weights": COMPLEXITY_WEIGHTS,
        },
        "summary": summary,
        "presets": public_items,
    }
    atomic_write_text(
        output_dir / "presets.json",
        json.dumps(payload, ensure_ascii=False, indent=2, sort_keys=True, allow_nan=False) + "\n",
    )
    write_csv(output_dir / "presets.csv", public_items)
    write_contact_sheet(output_dir / "contact-sheet.png", public_items, output_dir)
    atomic_write_text(output_dir / "index.html", build_html(public_items, summary))
    print(json.dumps(summary, ensure_ascii=False, indent=2, allow_nan=False))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())

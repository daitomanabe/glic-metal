#!/usr/bin/env python3
"""Generate a balanced 50-pattern spatial, original, and codec selection."""

from __future__ import annotations

import argparse
from datetime import datetime, timezone
import json
from pathlib import Path
import subprocess
import sys
from typing import Any

from evaluate_codec_glitch_videos import (
    analyze_frame_pairs,
    decode_sampled_frames,
    executable_path,
    pairwise_distances,
    probe_video,
    rank_records,
    score_visual_metrics,
    sha256_file,
)


SPATIAL = [
    ("legacy_block", 0.70, 0.50, 0.50),
    ("line_tear", 0.67, 0.97, 0.32),
    ("channel_shear", 0.55, 0.64, 0.24),
    ("analog_sync", 0.59, 0.04, 0.12),
    ("mirror_fold", 0.81, 0.98, 0.15),
    ("edge_echo", 0.46, 0.94, 0.20),
    ("bitplane_dither", 0.79, 0.28, 0.92),
    ("wave_warp", 0.68, 0.55, 0.48),
    ("poster_solar", 0.49, 0.36, 0.15),
    ("tile_shuffle", 0.72, 0.46, 0.62),
    ("vertical_tear", 0.68, 0.65, 0.45),
    ("diagonal_slip", 0.70, 0.72, 0.58),
    ("scanline_weave", 0.62, 0.38, 0.72),
    ("quad_mirror", 0.76, 0.84, 0.32),
]

ORIGINAL = [
    "bi0g4n1c", "vv02", "vv01", "colour_glow", "colour_waves_sharp",
    "webp", "vv17", "wtf", "bl33dyl1n3z-2", "beautifulwave",
    "abstract_expressionism", "colourful_disturbances",
    "constrctivist_minimal", "bl33dyl1n3z", "burn", "wtf2",
    "web_p_like", "lightblur",
]

CODEC = [
    "pframe_freeze_bursts", "timewarp_nonlinear", "bitrate_meltdown",
    "transplant_mosaic", "dropout_storm", "payload_rupture",
    "cascade_collapse", "idr_collapse", "chroma_flood",
    "resolution_staircase", "qp_erratic", "feedback_recursion",
    "polyphony_luma_choir", "intra_recursive_blocks",
    "residual_affine_rift", "grain_pulse", "skin_recursive",
    "concealment_regions",
]


def validate_bank() -> None:
    if (len(SPATIAL), len(ORIGINAL), len(CODEC)) != (14, 18, 18):
        raise ValueError("mixed selection must be 14 spatial + 18 original + 18 codec")
    labels = (
        [f"spatial__{row[0]}" for row in SPATIAL]
        + [f"original__{name}" for name in ORIGINAL]
        + [f"codec__{name}" for name in CODEC]
    )
    if len(labels) != 50 or len(labels) != len(set(labels)):
        raise ValueError("mixed selection must contain 50 unique labels")


def run(command: list[str]) -> None:
    print("+", " ".join(command), flush=True)
    subprocess.run(command, check=True)


def report_performance(
    report: dict[str, Any], probe: dict[str, Any], category: str
) -> dict[str, Any]:
    filter_report = report.get("filter") if isinstance(report.get("filter"), dict) else {}
    frames = int(report.get("processed_frames") or 0)
    output_frames = int(report.get("output_frame_count") or 0)
    observed = float(report.get("end_to_end_observed_fps") or 0.0)
    engine_fps = float(
        report.get("codec_engine_fps")
        or filter_report.get("stream_observed_fps")
        or filter_report.get("processing_fps")
        or 0.0
    )
    p95 = float(
        report.get("codec_latency_p95_milliseconds")
        or filter_report.get("stream_wall_p95_ms")
        or filter_report.get("max_process_ms")
        or 0.0
    )
    reasons = []
    if int(probe.get("width") or 0) < 960 or int(probe.get("height") or 0) < 540:
        reasons.append("output is smaller than 960x540")
    if frames < 120 or output_frames != frames or report.get("frame_count_preserved") is not True:
        reasons.append("frame count is not preserved or has fewer than 120 frames")
    if observed < 20.0 or engine_fps < 20.0:
        reasons.append("observed processing is below 20 fps")
    if p95 <= 0.0 or p95 > 50.0:
        reasons.append("p95/max processing latency exceeds 50 ms")
    if report.get("end_to_end_average_20fps_passed") is not True:
        reasons.append("reported end-to-end 20 fps gate failed")
    if category == "codec":
        if report.get("codec_reliability_passed") is not True:
            reasons.append("codec reliability gate failed")
        if any(
            int(report.get(key) or 0) != 0
            for key in (
                "codec_processing_errors", "codec_fallback_frames",
                "codec_watchdog_recoveries",
            )
        ):
            reasons.append("codec fallback, error, or recovery is nonzero")
    return {
        "hard_gate_passed": not reasons,
        "gate_reasons": reasons,
        "minimum_fps": 20.0,
        "observed_processing_fps": observed,
        "codec_engine_fps": engine_fps,
        "latency_p95_ms": p95,
        "output_width": int(probe.get("width") or 0),
        "output_height": int(probe.get("height") or 0),
        "processed_frames": frames,
        "output_frame_count": output_frames,
        "fallback_frames": int(report.get("codec_fallback_frames") or 0),
        "codec_errors": int(report.get("codec_processing_errors") or 0),
        "reliability_penalty": 0.0 if not reasons else 1.0,
    }


def write_html(path: Path) -> None:
    path.write_text("""<!doctype html><html lang="ja"><head><meta charset="utf-8"><meta name="viewport" content="width=device-width,initial-scale=1"><title>GLIC Metal — Mixed 50</title><style>
:root{color-scheme:dark;--bg:#090909;--panel:#131313;--line:#333;--text:#f3f3ed;--muted:#aaa;--green:#d8ff45;--blue:#79c8ff;--pink:#ff8fcf}*{box-sizing:border-box}body{margin:0;background:var(--bg);color:var(--text);font-family:-apple-system,BlinkMacSystemFont,"Segoe UI",sans-serif}header{position:sticky;top:0;z-index:3;padding:1rem 1.2rem;background:#090909ed;border-bottom:1px solid var(--line);backdrop-filter:blur(12px)}.top,.actions{display:flex;gap:.7rem;align-items:center;justify-content:space-between;flex-wrap:wrap}h1{margin:0;font-size:1rem;letter-spacing:.08em}.summary{color:var(--muted);font:12px ui-monospace,SFMono-Regular,monospace}.actions{justify-content:flex-start;margin-top:.7rem}button{padding:.45rem .7rem;border:1px solid var(--line);border-radius:999px;background:transparent;color:var(--text);cursor:pointer}button.active{border-color:var(--green);color:var(--green)}main{padding:1rem}.grid{display:grid;grid-template-columns:repeat(auto-fill,minmax(320px,1fr));gap:1rem}article{overflow:hidden;border:1px solid var(--line);background:var(--panel)}article.selected{border-color:var(--green);box-shadow:0 0 0 1px var(--green) inset}video{display:block;width:100%;aspect-ratio:16/9;background:#000;object-fit:cover}.body{padding:.8rem}.row{display:flex;align-items:center;justify-content:space-between;gap:.6rem}h2{margin:0;font:600 13px ui-monospace,SFMono-Regular,monospace}label{display:flex;gap:.4rem;color:var(--green);font-size:13px}.badge{display:inline-block;margin-top:.35rem;padding:.16rem .42rem;border:1px solid var(--line);border-radius:999px;color:var(--muted);font:10px ui-monospace,SFMono-Regular,monospace}.badge.codec{color:var(--pink)}.badge.spatial{color:var(--blue)}.badge.original{color:var(--green)}.metrics{display:grid;grid-template-columns:repeat(4,1fr);gap:.3rem;margin-top:.6rem}.metric{padding:.4rem;background:#090909}.metric span{display:block;color:var(--muted);font:9px ui-monospace,SFMono-Regular,monospace}.metric strong{font:12px ui-monospace,SFMono-Regular,monospace}.pass{color:var(--green)}@media(max-width:600px){.grid{grid-template-columns:1fr}}
</style></head><body><header><div class="top"><div><h1>GLIC—METAL / MIXED 50</h1><div id="summary" class="summary">loading…</div></div><button id="export">選択JSONをコピー</button></div><div class="actions"><button data-filter="all" class="active">ALL 50</button><button data-filter="spatial">SPATIAL 14</button><button data-filter="original">ORIGINAL 18</button><button data-filter="codec">CODEC 18</button><button id="all">表示中を全選択</button><button id="none">全解除</button></div></header><main><div id="grid" class="grid"></div></main><script>
const key='glic-metal-mixed-selection-v1',selected=new Set(JSON.parse(localStorage.getItem(key)||'[]'));let rows=[],filter='all';const fmt=(v,d=2)=>Number.isFinite(Number(v))?Number(v).toFixed(d):'—',visible=()=>rows.filter(x=>filter==='all'||x.category===filter),save=()=>localStorage.setItem(key,JSON.stringify([...selected]));function update(){document.querySelector('#summary').textContent=`50 patterns · spatial 14 · original 18 · codec 18 · ${selected.size} selected`}function render(){const grid=document.querySelector('#grid');grid.replaceChildren();for(const x of visible()){const p=x.performance||{},m=x.metrics||{},card=document.createElement('article');card.classList.toggle('selected',selected.has(x.label));card.innerHTML=`<video src="${x.video_url}" controls loop muted playsinline preload="metadata"></video><div class="body"><div class="row"><h2>${x.rank}. ${x.label.replace(/^[^_]+__/, '')}</h2><label><input type="checkbox" ${selected.has(x.label)?'checked':''}>採用</label></div><span class="badge ${x.category}">${x.category.toUpperCase()} · ${x.effect}</span><div class="metrics"><div class="metric"><span>GATE</span><strong class="${p.hard_gate_passed?'pass':''}">${p.hard_gate_passed?'PASS':'FAIL'}</strong></div><div class="metric"><span>FPS</span><strong>${fmt(p.codec_engine_fps,1)}</strong></div><div class="metric"><span>P95/MAX</span><strong>${fmt(p.latency_p95_ms,1)}ms</strong></div><div class="metric"><span>CHANGED</span><strong>${fmt(m.changed_ratio*100,1)}%</strong></div></div></div>`;const box=card.querySelector('input');box.onchange=()=>{box.checked?selected.add(x.label):selected.delete(x.label);card.classList.toggle('selected',box.checked);save();update()};grid.append(card)}update()}document.querySelectorAll('[data-filter]').forEach(b=>b.onclick=()=>{filter=b.dataset.filter;document.querySelectorAll('[data-filter]').forEach(x=>x.classList.toggle('active',x===b));render()});document.querySelector('#all').onclick=()=>{visible().forEach(x=>selected.add(x.label));save();render()};document.querySelector('#none').onclick=()=>{selected.clear();save();render()};document.querySelector('#export').onclick=async e=>{const chosen=rows.filter(x=>selected.has(x.label)).map(x=>({name:x.label,category:x.category,effect:x.effect,controls:x.controls}));await navigator.clipboard.writeText(JSON.stringify({mixed_glitch_patterns:chosen},null,2));const b=e.currentTarget,o=b.textContent;b.textContent='コピーしました';setTimeout(()=>b.textContent=o,1200)};fetch('./ranking.json',{cache:'no-store'}).then(r=>r.json()).then(d=>{rows=d.ranking;render()});
</script></body></html>""", encoding="utf-8")


def parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(description="Generate a balanced mixed 50-pattern review site.")
    parser.add_argument("input", nargs="?", type=Path)
    parser.add_argument("--output-dir", type=Path, default=Path("test-videos/glitch-selection-50"))
    parser.add_argument("--codec-dir", type=Path, default=Path("test-videos/codec-glitch"))
    parser.add_argument("--overwrite", action="store_true")
    parser.add_argument("--skip-render", action="store_true",
                        help="Reuse existing control, spatial, and original videos/reports.")
    parser.add_argument("--selftest", action="store_true")
    return parser.parse_args()


def main() -> int:
    args = parse_args()
    validate_bank()
    if args.selftest:
        print("PASS mixed glitch selection patterns=50 spatial=14 original=18 codec=18")
        return 0
    if args.input is None or not args.input.is_file():
        raise SystemExit("input video is required and must exist")
    root = Path(__file__).resolve().parent.parent
    output = args.output_dir.resolve()
    codec_dir = args.codec_dir.resolve()
    output.mkdir(parents=True, exist_ok=True)
    process = root / "scripts" / "process_video.py"
    overwrite = ["--overwrite"] if args.overwrite else []
    common = ["--width", "960", "--height", "540", "--fps", "30"]
    manifest: list[dict[str, Any]] = []

    control = output / "control.mp4"
    if not args.skip_render:
        run([sys.executable, str(process), str(args.input.resolve()), str(control),
             "--processing-mode", "compat_realtime", "--passthrough", *common,
             "--report", str(output / "control.json"), *overwrite])
    elif not control.is_file():
        raise SystemExit(f"existing control is missing: {control}")

    for index, (family, amount, scale, rate) in enumerate(SPATIAL):
        label = f"spatial__{family}"
        video, report = output / f"{label}.mp4", output / f"{label}.json"
        seed = 0x4D495800 + index + 1
        if not args.skip_render:
            run([sys.executable, str(process), str(args.input.resolve()), str(video),
                 "--processing-mode", "compat_realtime", "--preset", "default",
                 "--effect-family", family, "--effect-amount", str(amount),
                 "--effect-scale", str(scale), "--effect-rate", str(rate),
                 "--seed", hex(seed), *common, "--report", str(report), *overwrite])
        elif not video.is_file() or not report.is_file():
            raise SystemExit(f"existing spatial candidate is missing: {label}")
        manifest.append({"label": label, "category": "spatial", "effect": family,
                         "controls": {"amount": amount, "scale": scale, "rate": rate, "seed": seed},
                         "video": str(video), "video_url": f"./{video.name}", "report": str(report)})

    for preset in ORIGINAL:
        label = f"original__{preset}"
        video, report = output / f"{label}.mp4", output / f"{label}.json"
        if not args.skip_render:
            run([sys.executable, str(process), str(args.input.resolve()), str(video),
                 "--processing-mode", "original_visual", "--preset", preset,
                 *common, "--report", str(report), *overwrite])
        elif not video.is_file() or not report.is_file():
            raise SystemExit(f"existing original candidate is missing: {label}")
        manifest.append({"label": label, "category": "original", "effect": preset,
                         "controls": {"preset": preset}, "video": str(video),
                         "video_url": f"./{video.name}", "report": str(report)})

    codec_bank = json.loads((codec_dir / "candidate-bank.json").read_text(encoding="utf-8"))
    codec_by_name = {row["name"]: row for row in codec_bank["candidates"]}
    for name in CODEC:
        source = codec_by_name[name]
        video, report = codec_dir / source["video"], codec_dir / source["report"]
        if not video.is_file() or not report.is_file():
            raise SystemExit(f"codec candidate is missing: {name}")
        manifest.append({"label": f"codec__{name}", "category": "codec",
                         "effect": source["effect"],
                         "controls": {key: source[key] for key in ("amount", "rate", "feedback", "seed")},
                         "video": str(video), "video_url": f"../codec-glitch/{video.name}",
                         "report": str(report)})

    ffmpeg, ffprobe = executable_path("ffmpeg"), executable_path("ffprobe")
    control_frames = decode_sampled_frames(ffmpeg, control, sample_fps=6.0,
                                           max_frames=90, width=256, height=144)
    records = []
    for item in manifest:
        video, report_path = Path(item["video"]), Path(item["report"])
        probe = probe_video(ffprobe, video)
        frames = decode_sampled_frames(ffmpeg, video, sample_fps=6.0,
                                       max_frames=90, width=256, height=144)
        metrics, fingerprint = analyze_frame_pairs(control_frames, frames, 12.0 / 255.0)
        report = json.loads(report_path.read_text(encoding="utf-8"))
        performance = report_performance(report, probe, item["category"])
        records.append({**item, "video_sha256": sha256_file(video),
                        "report_sha256": sha256_file(report_path), "probe": probe,
                        "metrics": metrics, "fingerprint": fingerprint,
                        "visual_scores": score_visual_metrics(metrics),
                        "performance": performance,
                        "status": "ELIGIBLE" if performance["hard_gate_passed"] else "INELIGIBLE"})
    ranking = rank_records(records)
    pairs = pairwise_distances(records)
    payload = {
        "schema": "glic-mixed-glitch-selection-v1",
        "generated_at": datetime.now(timezone.utc).isoformat(),
        "policy": {"total": 50, "category_quotas": {"spatial": 14, "original": 18, "codec": 18},
                   "realtime_gate": "960x540, 120+ frames, 20fps, p95/max <= 50ms",
                   "ranking": "hard-gate then greedy max-min dry/wet fingerprint diversity"},
        "summary": {"candidate_count": 50,
                    "eligible_count": sum(x["performance"]["hard_gate_passed"] for x in ranking),
                    "categories": {"spatial": 14, "original": 18, "codec": 18}},
        "ranking": ranking, "pairwise_distances": pairs,
    }
    (output / "ranking.json").write_text(json.dumps(payload, indent=2) + "\n", encoding="utf-8")
    (output / "selection.json").write_text(json.dumps({"schema": payload["schema"], "patterns": manifest}, indent=2) + "\n", encoding="utf-8")
    write_html(output / "index.html")
    print(f"PASS mixed selection eligible={payload['summary']['eligible_count']}/50 preview={output / 'index.html'}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())

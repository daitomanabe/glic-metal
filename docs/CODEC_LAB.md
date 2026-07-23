# Realtime Crossbreed and Offline Codec Lab

[日本語](#日本語) | [English](#english)

## 日本語

GLIC Metalはcodec系グリッチを、次の3つの安全境界へ分けます。

| Class | Effect数 | Backend | realtime |
|---|---:|---|---:|
| Realtime Codec Glitch | 28 | VideoToolbox clean encode/decode + GPU再構成 | 960×540・20fps gate |
| Offline Packet Lab | 8 | FFmpeg bitstream filter + 隔離救済decode | 非対応 |
| Offline Syntax / Analysis Lab | 18 | 実codec cycle + 復号再構成・解析・探索 | 非対応 |

### Realtime Crossbreed

既存18 effectへ、次の10 effectを追加しています。

- `dual_codec_crossbreed`
- `codec_pingpong`
- `gop_accordion`
- `bframe_braid`
- `plane_split_codec`
- `roi_quality_islands`
- `codec_phase_mosaic`
- `encoder_hot_swap`
- `pts_rubberband`
- `bitrate_raster`

20fpsのhard floorを守るため、これらは複数hardware encoderを毎frame並列起動しません。
選択されたH.264 / HEVC / ProResのclean encode/decode結果、複数時点の復号履歴、
codec品質変化、GPU領域合成を使う低遅延適応です。真の複数codec decode合成は
offlineの`decoder_fingerprint_ensemble`、真の異種codec直列処理は
`cross_codec_chain`が担当します。

```bash
python3 scripts/process_video.py input.mov output.mp4 \
  --processing-mode codec_glitch \
  --codec-format hevc \
  --codec-effect dual_codec_crossbreed \
  --codec-amount 0.82 --codec-rate 0.58 --codec-feedback 0.76 \
  --width 960 --height 540 --fps 30 --overwrite
```

### Syntax Lab

`scripts/process_codec_lab.py`は12種類のcodec構造グリッチを提供します。

- motion field: `motion_vector_vortex`、`motion_vector_mirror`、
  `motion_vector_quantizer`、`motion_vector_freeze`
- residual/transform: `residual_sign_flip`、`residual_band_gate`、
  `transform_block_transplant`
- reference/decoder: `reference_graph_swap`、`entropy_state_puncture`、
  `loop_filter_oscillator`
- modern codec: `av1_film_grain_instrument`、`av2_optical_flow_wound`

motion vector、residual、reference graph操作は、汎用decoderを危険に改造する代わりに
復号再構成層で実装します。JSONの`implementation_level`は
`decoded_reconstruction_proxy`と明記され、native syntax hookとは主張しません。
ただし処理後は指定codecで実際にencode/decodeされ、bitstream、SHA-256、probe、
処理logが残ります。AV2 effectは公式AVM v1.0.0を実際に通し、AVMが無ければ
fail-closedします。

```bash
python3 scripts/process_codec_lab.py input.mov vortex.mp4 \
  --effect motion_vector_vortex --codec hevc \
  --amount 0.76 --rate 0.58 --feedback 0.70
```

### Semantic / Analysis Lab

- `semantic_reference_retarget` — 動き領域をsemantic fallback maskとして履歴を変更
- `depth_motion_rift` — 決定的なmonocular depth proxyで変位量を変更
- `decoder_fingerprint_ensemble` — H.264 / HEVC / ProResの実decode差を合成
- `cross_codec_chain` — AV1 → VP9 → HEVC → ProResの実世代処理
- `audio_codec_orchestra` — source audio RMSから領域、noise、変位を制御
- `evolutionary_codec_search` — 上記effectと制御値をローカル自動探索

semantic modelやdepth modelを同梱しないため、最初の2 effectは明示された決定的proxy
です。モデルを使用したと偽装しません。

### Token-free evolutionary search

探索はLLMや外部APIを呼びません。各候補を実動画へ適用し、MAE、changed ratio、
edge差、時間差、decode可否を測定し、品質とnoveltyの両方で最大50件を保持します。
各iteration後に`search-state.json`を書き、`--resume`で継続できます。

```bash
python3 scripts/evolutionary_codec_search.py input.mov \
  --output-dir search-runs/codec-lab \
  --budget 64 --archive-size 50 \
  --width 480 --height 270 --fps 15 --max-frames 90

python3 scripts/evolutionary_codec_search.py input.mov \
  --output-dir search-runs/codec-lab --budget 128 --resume
```

正規effect名、backend区分、implementation levelは
`resources/codec-lab-effects.json`が機械可読な基準です。

## English

Codec effects are split into three explicit safety classes: 28 clean-decode
VideoToolbox realtime effects, eight isolated packet operations, and eighteen
offline syntax/analysis/search workflows.

The ten Realtime Crossbreed effects are low-latency adaptations. They use one
selected hardware codec plus decoded history and GPU reconstruction; they do
not pretend to run two hardware codecs per frame. True multi-decoder blending
and cross-codec generations are available offline.

The Syntax Lab performs a real encode/decode cycle for every output while
labeling decoded motion/residual/reference reconstruction honestly as a proxy,
not as a native bitstream syntax hook. AV2 processing uses the pinned official
AVM tools and fails closed when unavailable.

The evolutionary search is deterministic, resumable, and token-free. It
measures visible change and novelty locally and retains a bounded archive.
Use `resources/codec-lab-effects.json` as the machine-readable capability
catalog.

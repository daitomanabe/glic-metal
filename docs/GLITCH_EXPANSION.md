# Glitch Expansion Catalog and Validation

[日本語](#日本語) | [English](#english)

## 日本語

この文書は、追加グリッチの実装境界、実動画評価、外部アプリからの選択方法を
一か所にまとめたものです。正規の機械可読一覧は
`resources/codec-lab-effects.json`、呼び出し契約は
`resources/integration-manifest.json`です。

### 実装レベルの読み方

- `native_*`、`official_*_generation_cycle`: 圧縮codec、NAL/OBU、transport、
  VUI/SEI、packetを実際に生成・解析・変更します。
- `decoder_exported_*`、`decoded_*_rewrite`、`decoded_reconstruction_proxy`:
  decoderが出したvector、係数、pixelを変更します。entropy-coded payloadを直接
  書き換えたとは主張しません。
- `videotoolbox_*_plus_*_reconstruction`: realtime向けです。VideoToolboxで
  clean encode/decodeした履歴をCoreImage/Metalで再構成し、圧縮sample byteは
  破損しません。
- `rfc6184_h264_offline_packet_model_not_network_capture`: RTP sequenceを扱う
  決定的なoffline packet modelであり、live network captureではありません。

C/C++ホストは各realtime effectについて
`glic_codec_glitch_effect_implementation_level()`を呼べます。offline toolは同じ
意味の`implementation_level`をJSONへ書きます。

### Realtime追加8種

| Effect | 960×540 stream fps | p95 ms | 入力との差 | 技術QA |
|---|---:|---:|---|---|
| `plane_time_split` | 117.883 | 7.563 | SUBTLE、MAE 15.76 | PASS |
| `reference_atlas` | 40.709 | 25.311 | STRONG、MAE 52.36 | PASS |
| `flow_lattice` | 165.905 | 7.030 | STRONG、MAE 34.46 | PASS |
| `scan_order_fold` | 188.523 | 5.525 | STRONG、MAE 38.57 | PASS |
| `regional_gop_clock` | 73.283 | 23.812 | VISIBLE、MAE 17.05 | PASS |
| `entropy_feedback` | 118.547 | 9.417 | VISIBLE、MAE 17.82 | PASS |
| `rolling_time_shutter` | 68.638 | 26.698 | VISIBLE、MAE 20.45 | PASS |
| `asymmetric_plane_codec` | 107.123 | 9.467 | SUBTLE、MAE 23.93 | PASS |

2026-07-24にH.264 VideoToolbox、960×540、30fps、120 frameの同一実動画で測定
しました。8/8がhardware encode/decode、frame survival、20fps/p95 50ms gateを
通過し、video-render-qaでもdecode、motion、exposure、color、complexity、
lightingがPASSでした。`plane_time_split`と`asymmetric_plane_codec`は広い画素を
変えますがSSIMが高く、差分評価では意図的に`SUBTLE`と表示します。

### Offline / native追加系統

| 系統 | Effect / codec | 実装境界 | realtime |
|---|---|---|---|
| Native syntax | MPEG-2 motion vector / qDCT 8 effect | FFglitch entropy transplication | なし |
| Structured AV1 | tile group、film grain seed、reference slot | traceで対応付けたOBU/field操作 | なし |
| Structured HEVC | temporal layer dropout / reorder | `nuh_temporal_id`単位のdrop/reorder | なし |
| Cross stream | H.264 / HEVC / AV1 unit transplant | 圧縮frame unit移植 | なし |
| Transport | MPEG-TS continuity、RTP sequence、HLS splice | native TS/HLS、offline RFC6184 model | なし |
| Metadata | color VUI、HDR metadata pulse | H.264/HEVC VUI、HEVC mastering-display SEI | なし |
| Generation | AV1、AV2、HEVC、VP9、ProRes、VVC、Theora、Dirac | 実codec encode/decode世代処理 | なし |
| Cross-modal | decoder disagreement、audio packet resonance | 3 decoderのpixel差、Opus packet size/PTS | なし |

VVCはpinned Fraunhofer VVenC v1.14.0、AV2はpinned AOMedia AVM v1.0.0を使い、
見つからない場合は別codecへ置換せずfail-closedします。TheoraはFFmpeg
`libtheora`、DiracはFFmpeg VC-2/Diracです。各generationのbitstream、SHA-256、
probe、処理時間、decode中間映像を残します。

実動画スモーク評価ではVVC、Theora、Dirac、color VUI、HDR metadata、
MPEG-TS、RTP model、HLS、decoder disagreement、Opus packetの全出力がdecode
可能でした。初回QAでDiracの不正なslice端による全灰色化とmetadata previewの
過剰clipを検出し、VC-2内部寸法を32×16へ整列、最低bit budgetを確保し、
metadata-aware previewだけを安全なdisplay rangeへ正規化しました。最終再評価では
修正3本すべて30/30 frame、repeat 0、技術QA PASSです。圧縮bitstreamとmetadata
証跡自体は表示用正規化より前に保持されます。

MPEG-2 native syntaxはFFglitch 0.10.2の`mv` / `q_dct` export/importを使います。
2026-07-24の実動画45 frame評価では、motion-vector vortexとqDCT sign flipの
両方が45/45 frameを保持し、source/damaged SHA-256が変化しました。同一MPEG-2
controlとの差分はそれぞれVISIBLE（MAE 20.72、changed>=10 40.8%）とVISIBLE
（MAE 11.58、changed>=10 38.3%）でした。両方がvideo-render-qaのdecode、
motion、露出、色、複雑度、lightingをPASSし、repeat/frozen pairは0です。8/8
effectの追加スモークも24/24 frameとbitstream hash変化を保持しました。
H.264/HEVC直接entropy編集は未実装でfail-closedします。

### 外部アプリから使う

Realtime:

```c
const char *level =
    glic_codec_glitch_effect_implementation_level(effect);
glic_codec_glitch_controls controls;
glic_codec_glitch_controls_init(&controls);
controls.effect = effect;
glic_codec_glitch_set_controls(codec_context, &controls);
```

Offline:

```bash
python3 Tools/process_native_syntax_glitch.py input.mov output.mp4 \
  --codec mpeg2 --effect compressed_motion_vector_vortex \
  --report output.json

python3 Tools/process_structured_codec_glitch.py input.mov output.mp4 \
  --codec hevc --effect temporal_layer_dropout --report output.json

python3 Tools/process_transport_glitch.py input.mov output.mp4 \
  --effect mpegts_continuity_fracture --report output.json

python3 Tools/process_metadata_glitch.py input.mov output.mp4 \
  --codec hevc --effect hdr_metadata_pulse --report output.json
```

capture/render callbackからoffline toolを呼ばず、child processのexit statusとJSONを
完了契約にしてください。effect名とcodec対応はcatalogから読み、未知の値は
fail-closedします。

## English

This document is the consolidated capability and validation record for the
glitch expansion. `codec-lab-effects.json` is the canonical effect catalog and
`integration-manifest.json` is the invocation contract.

The eight new realtime effects use clean VideoToolbox encode/decode history and
CoreImage/Metal reconstruction. They do not mutate compressed payload bytes.
All eight passed the 960x540, 120-frame, 20 fps/p95 50 ms hardware gate and
technical video QA. Difference analysis deliberately labels two visually broad
but structurally similar results as `SUBTLE`.

Offline workflows perform the declared codec, syntax, transport, metadata, or
cross-modal operation and make no realtime claim. Native/official labels mean
the named compressed structure or reference codec is actually used. Decoded
proxy labels mean the operation is performed on decoder-exported data or
pixels. The RTP effect is an offline RFC 6184 packet model, not a live capture.

The Native Compressed Syntax Lab uses FFglitch 0.10.2 transplication for eight
MPEG-2 motion-vector/qDCT effects. Actual-video validation retained 45/45
frames for both representative paths; motion-vector vortex measured MAE 20.72
with 40.8% of pixels changed by at least 10, and qDCT sign flip measured MAE
11.58 with 38.3% changed. Both were `VISIBLE`, passed decode, motion, exposure,
color, complexity, and lighting QA, and had zero repeated/frozen pairs. An
additional all-effect smoke retained 24/24 frames and changed the bitstream
hash for 8/8 effects. H.264/HEVC direct entropy edits remain unavailable and
fail closed.

Actual-video QA caught and led to fixes for a VC-2 partial-slice neutral-frame
collapse and excessive clipping in metadata-aware review previews. The final
Dirac and two metadata previews each retained 30/30 frames with no repeated
pairs and passed technical QA. Original compressed evidence is retained before
display normalization.

Downstream applications should query
`glic_codec_glitch_effect_implementation_level()` for realtime effects and
preserve each offline JSON `implementation_level`. Run offline tools in child
processes and validate names through the bundled catalogs.

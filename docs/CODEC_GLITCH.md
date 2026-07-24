# Codec Glitch

[日本語](#日本語) | [English](#english)

Codec Glitch is the macOS-only, stateful H.264 / HEVC / ProRes processing lane
in GLIC Metal. The eight offline codec generation workflows are documented in
[MULTICODEC_GLITCH.md](MULTICODEC_GLITCH.md).
It is intentionally separate from the GLIC file codec and both realtime image
lanes. The encoder and decoder are provided by VideoToolbox; Metal-compatible
`CVPixelBuffer` pools and a Metal-backed post/composite path keep frames in a
GPU-friendly native video pipeline.

## 日本語

### 位置づけ

Codec Glitchは、入力映像をVideoToolboxでH.264、HEVC、またはProRes 422へ
encode/decodeし、その処理中に
codec品質の変調、意図的なframe hold、またはdecode履歴を使う前段／後段合成を行う
macOS専用の非同期処理レーンです。圧縮video payload byteは変更しません。

このモードは、次の3経路とは別物です。

| 経路 | 主な目的 | preset / effect |
|---|---|---|
| ファイルcodec | 画像と`.glic`ファイルのencode/decode | 上流GLICのcodecパラメータ |
| `original_visual` | 原作スタイルに近いリアルタイム再構成 | 監査済み37 preset、Strict / Fast Match |
| `compat_realtime` | 高速なGPU合成と全144 presetの視覚近似 | GLIC presetと14 realtime family |
| `codec_glitch` | H.264の時間参照・量子化・encode済みframe cadence・decode済みframe履歴を使う動画グリッチ | 下記36 effect、GLIC presetとは無関係 |

`codec_glitch`で`--preset`、`--preset-semantics`、検索recipeの
`--canonical`を変換して使うことはありません。原作GLICとのpixel一致も主張しません。
圧縮codecとOSの挙動を利用するため、同じseedでもmacOS、SoC、VideoToolboxの版に
よって細部が変わる場合があります。

### 処理構成

1. ホストがBGRAの`CVPixelBufferRef`を非同期engineへsubmitします。
2. `codec_feedback`は前回のcodec decode結果をencode入力へ合成し、
   `resolution_hop`はencode前に入力を縮小します。
3. VideoToolboxのH.264 hardware encoderが`RealTime` modeでpacketを生成します。QP、bitrate、
   世代数を使うeffectは、このencode/decode段を強く変調または反復します。
4. `pframe_loss`と`idr_starvation`は選択したencode済みframeを意図的にholdし、
   直前の正常なdecode結果をrepeatします。
5. それ以外のH.264 packetはbyteを変更せず、VideoToolbox hardware decoderが映像
   frameを復元します。
6. `slice_dropout`、`slice_transplant`、`payload_xor`、
   `chroma_codec_echo`と研究版6 effect、縮小復元／pixel化はMetal互換pixel-buffer poolと
   Metal-backed CoreImage pathを使います。`reference_timewarp`は4〜12 frameへ設定できる
   decode済み`CVPixelBuffer`履歴から過去frameを選択します。
7. 完成frameはcallback、または有界poll queueへ返ります。意図したholdと障害時の
   fallbackは別々のflagで識別できます。

H.264のencode/decode自体をMetal shaderで置き換えているわけではありません。
圧縮処理はVideoToolbox、後段の画像処理とGPU連携がMetal-backedです。
`prepare`はqueue、pool、通常stageのhardware encoderを準備してbackendを検証します。
QP、cascade、縮小用encoderはeffectが最初に使う時、decoderは最初のencode済みsampleを
受け取った時に遅延生成されるため、全stageを起動時に予約しません。

VideoToolboxの`RealTime`は常に有効です。low-latency rate controlも既定で有効ですが、
既定average bitrateは4,000,000 bpsです。動的bitrateは
`min(averageBitRate, width * height * fps / 4)`未満へ下げない安全floorを設け、
強いbitrate変更でhardware encoderがframeを落とすことを防ぎます。

### 36 effect

| effect名 | 主な操作 |
|---|---|
| `qp_pump` | frameごとのQPを広い範囲で時間変調し、精細さとブロック化を往復させる |
| `bitrate_crush` | amountに応じて安全floorまでbitrateを下げ、floor到達後はMetal-backed圧縮模様も加えてcodec由来の劣化を見せる |
| `slice_dropout` | 現在のcodec decode frameの選択した水平slice-rowを直前のcodec decode frameで置換し、row dropout / holdを作る。VCL payloadは変更しない |
| `slice_transplant` | 現在のcodec decode frameへ、codecでdecode済みの履歴frameから選択した水平帯を位置をずらしてMetal-backed CoreImageで合成する。VCL payloadは変更しない |
| `pframe_loss` | 選択した非IDR encode済みframeを意図的にholdし、直前のdecode結果をrepeatして時間方向の停止を作る |
| `idr_starvation` | 選択したIDR encode済みframeを意図的にholdし、clean refreshのcadenceを減らす。watchdogの復旧IDRは抑止しない |
| `payload_xor` | clean decode後にposterize、RGB channelの決定的な組み替え、位置をずらしたmacroblock状tileをMetal-backed CoreImageで合成する。名称は視覚的なdigital damageを表し、payload byteへXORしない |
| `reference_timewarp` | 4〜12 frameへ設定できるdecode済み`CVPixelBuffer`履歴から古いframeを決定的に選び、時間を巻き戻す。圧縮P packetは再利用しない |
| `codec_feedback` | 直前のdecode結果を現在frameへ混ぜ、再encodeする |
| `generation_cascade` | encode/decodeを2〜3世代重ね、後の世代ほどbitrate圧力とMetal-backed圧縮模様を強めて世代劣化を作る |
| `resolution_hop` | 1/2または1/4解像度のcodec段を経由し、pixel化を加えて元サイズへ戻す |
| `chroma_codec_echo` | 過去のdecode色成分を時間差で合成し、色の残像を作る |
| `temporal_polyphony` | 現在frameの輝度構造を領域maskとして、近い履歴と遠い履歴を選び分け、複数の時間記憶を同一frameへ合成する |
| `intra_cannibalism` | decode済みframe内のblockを別位置へ繰り返しcopyし、後のcopyが先のcopyを再利用できる再帰的な自己参照を作る |
| `residual_rift` | 直前のdecode frameを予測として現在frameとの差分を作り、位置をwarpした予測へ同じ残差を再合成する |
| `codec_grain_synth` | Metal-backed random fieldをgrainとして生成し、amount、rate、feedbackから強度、時間変調、粒径を制御する |
| `recursive_codec_skin` | 直前の完成frameへnoise reduction、sharpen、色調整を適用して現在frameへfeedbackし、復元filterの反復に似た表皮を作る |
| `concealment_choreography` | 近い／遠いdecode履歴を複数の移動領域だけへ戻し、packetを落とさずに領域別concealmentを演出する |
| `dual_codec_crossbreed` | codec圧縮模様と時間差chroma復元を領域合成する低遅延crossbreed |
| `codec_pingpong` | 近い／遠いcodec decode履歴を短周期で往復する |
| `gop_accordion` | codec劣化と遠い履歴holdの周期を伸縮してGOP burstを近似する |
| `bframe_braid` | 近い／遠い履歴の表示順を領域別に編み込む |
| `plane_split_codec` | 現在の輝度と時間差の色成分を強く分離して再合成する |
| `roi_quality_islands` | 動く複数領域だけへ別時点のcodec品質を戻す |
| `codec_phase_mosaic` | 再帰block copyとcodec restoration履歴をtile状に合成する |
| `encoder_hot_swap` | 圧縮模様、chroma履歴、grain復元を周期的に切り替える |
| `pts_rubberband` | 履歴mixを強弱させ、局所停止と追いつきを作る |
| `bitrate_raster` | 走査する履歴帯へcodec圧縮模様を重ねる |
| `plane_time_split` | 輝度と色planeを異なるdecode時点から再構成する |
| `reference_atlas` | decode履歴を複数領域へ配置して参照frame atlasを作る |
| `flow_lattice` | 決定的な格子変位で時間履歴をwarpする |
| `scan_order_fold` | scan順を視覚モデルにした帯状foldで履歴を再配置する |
| `regional_gop_clock` | 領域ごとに異なる周期でdecode履歴を選択する |
| `entropy_feedback` | 局所複雑度をfeedback量へ写像して履歴を再合成する |
| `rolling_time_shutter` | 走査位置ごとに異なる過去frameを選択する |
| `asymmetric_plane_codec` | 輝度と色の縮小率・時間を非対称に再構成する |

最後の6 effectは、codec predictionを表現媒体にする研究ガイドを、公開VideoToolbox APIの
安全境界へ合わせて実装したリアルタイム版です。semantic segmentation model、外部depth、
optical flow、audio FFTは必須にせず、現在frameの輝度構造、決定的な領域mask、
decode履歴を使います。`intra_cannibalism`はIntraBC vector自体、`residual_rift`は圧縮
residual係数自体、`recursive_codec_skin`はAV1 CDEF / restoration bitstream自体を
書き換えません。後続10 effectは、1つの選択codecとdecode履歴を使うRealtime
Crossbreedです。複数encoderを毎frame並列起動するとは主張しません。AV2 motion
refinementの再構成版はofflineの`av2_optical_flow_wound`として
[CODEC_LAB.md](CODEC_LAB.md)に分離しています。最後の8 effectもdecode履歴と
CoreImage/Metal再構成であり、名称に含まれるplane、flow、scan、entropyは
native compressed-field hookを意味しません。

`amount`、`rate`、`feedback`は0〜1です。effect固有のQP、bitrate、世代数、
縮小率はC APIでも設定できます。engineは値を安全範囲へclampします。

### 安全境界と自動復旧

- 外部H.264 bitstreamを受け取って変更するAPIはありません。VideoToolboxが生成した
  H.264 packetも、全effectでVCL byteを変更せずdecoderへ渡します。
- `slice_dropout`、`slice_transplant`、`payload_xor`、
  `reference_timewarp`と研究版6 effect、Realtime Crossbreed 10 effect、
  最後の8 effectはclean decode後の`CVPixelBuffer`、または
  その有界履歴だけへ作用します。圧縮VCL sliceの削除・移植、payload XOR、
  過去P packetの再投入は
  行いません。
- `pframe_loss`と`idr_starvation`だけは選択したencode済みframeを意図的にholdします。
  この出力はC++の`intentionalRepeat`、C ABIの`intentional_repeat_frame`で識別でき、
  `repeatedPreviousFrame` / `repeated_previous_frame`もtrueになります。
- 意図しないcodec失敗時はlast-good frameをfallbackとして繰り返します。最初のdecode成功
  より前なら、retain済みのfull-size入力を出します。いずれも
  `non_intentional_fallback_frame=true`で、意図的holdと区別できます。
- 実際の連続失敗をwatchdogが検出すると、既存sessionの次frameをforced IDRにします。
  watchdogはsessionを破棄しません。明示的な`glic_codec_glitch_reset()`だけがdrain後に
  codec sessionを再構築し、decode履歴を消去します。
- submit queue、callback出力queue、poll出力queueはいずれも有界です。追いつかない入力はbackpressureとして
  dropし、遅延を無制限に蓄積しません。
- C++の`packetWasModified`とABI互換の`packet_was_modified`は、この安全な実装では
  falseです。
  `intentional_packet_drops`は既存field名を維持し、意図的にholdしたencode済みframe数を
  表します。
- `glic_codec_glitch_stats`と動画JSONにはintentional repeat、非意図的fallback、
  codec error、watchdog recovery、hardware codec、latencyの値が残ります。
  `codec_errors`はencode、sample抽出、decode、timeoutを含む全codec処理errorです。
  ABI互換の`poll_queue_drops`はcallback/pollどちらの出力queueで失われたframeも合算します。

これは自己生成streamをアプリ内で視覚効果へ使うための境界です。任意の第三者動画、
保存済みH.264、ネットワークstreamを破損する汎用bitstream editorではありません。

### 性能条件

標準の設計・計測条件は960×540、30fpsです。30fpsでは1 frameあたり33.33ms以内を
目標にし、別処理と同時に動く実運用では20fps、p95 50msをhard floorとします。
`generation_cascade`など複数世代を使うeffectは他より重くなります。
新しいstage/sessionだけはhardware起動を許容するencode 500ms / decode 300msのdeadlineを
使い、定常処理へ入るとencode 100ms / decode 45msへ戻します。該当出力は
`codec_warmup_frame=true`となり、filterの定常latency percentileから除外されます。

これは全Macでの保証値ではありません。`glic_codec_glitch_filter`のJSONは、非意図的な
fallback、codec error、watchdog recovery、backpressure drop、output queue dropが
すべて0の場合だけ`reliability_passed=true`にします。意図的holdは
`intentional_repeat_frames`へ別集計され、このreliability gateを失敗させません。
hardware encoder/decoder、960×540以上、最低120 frame、実測20fps、p95 50ms以下、
stream 20fps以上も満たした場合だけ`realtime_20fps_passed`をtrueにします。
`process_video.py`はfilter処理frame数とencode/mux後のframe数が一致しない結果も拒否します。
30fps gateは同じ信頼性条件に加えて30fps、p95 33.334ms以下を要求します。
公開effect設定は対象machineで実動画を使って再計測してください。

2026-07-23にApple M5 Maxで、5.53秒・166 frameの実写入力を960×540 / 30fpsへ
変換して新6 effectを測定しました。全6件でframe数を維持し、hardware encode/decode、
非意図的fallback 0、codec error 0、20fps hard gate合格でした。

| effect | end-to-end fps | codec p95 |
|---|---:|---:|
| `temporal_polyphony` | 50.02 | 8.25ms |
| `intra_cannibalism` | 49.82 | 12.36ms |
| `residual_rift` | 60.34 | 7.84ms |
| `codec_grain_synth` | 61.05 | 6.65ms |
| `recursive_codec_skin` | 60.06 | 11.88ms |
| `concealment_choreography` | 58.30 | 7.43ms |

### 動画を処理する

先にmacOSのRelease buildを作成します。

```bash
cmake -S . -B build -DCMAKE_BUILD_TYPE=Release
cmake --build build --target glic_codec_glitch_filter --parallel
```

`process_video.py`はFFmpegで入力をBGRAへdecodeし、1つのCodec Glitch engineを
stream全体で再利用した後、元音声を戻します。

```bash
python3 scripts/process_video.py input.mov output-codec.mp4 \
  --processing-mode codec_glitch \
  --codec-effect slice_transplant \
  --codec-amount 0.52 --codec-rate 0.34 --codec-feedback 0.58 \
  --seed 0x474c4943 \
  --width 960 --height 540 --fps 30 \
  --report output-codec.json --overwrite
```

36 effect名は`--codec-effect`へそのまま指定できます。このモードはmacOSと
VideoToolbox hardware codecを必須とし、`--backend cpu`を拒否します。

raw BGRA pipelineを組む場合は、filterを直接使えます。

```bash
ffmpeg -i input.mov -f rawvideo -pix_fmt bgra - \
  | ./build/glic_codec_glitch_filter \
      --width 960 --height 540 --fps 30 \
      --effect payload_xor --amount 0.16 --rate 0.30 \
      --stats-json codec-stats.json \
  | ffmpeg -f rawvideo -pix_fmt bgra -s 960x540 -r 30 -i - output.mp4
```

複数effect動画は、無加工controlと各`process_video.py` JSONを使って可視差と多様性を
rankingできます。MAE、変化率、輝度、色差、edge、時間差を測り、reliability hard gateを
通った候補だけをfingerprintのmax-min距離で並べます。

全36 effectを異なる強度、時間周期、feedback、seedで展開した最大50候補をまとめて
生成する場合は、candidate bank generatorを使います。動画、処理report、ranking、
選択状態を保存するcheckbox式`index.html`を同じ出力folderへ生成します。

```bash
python3 scripts/generate_codec_glitch_candidate_bank.py input.mov \
  --output-dir test-videos/codec-glitch \
  --filter-bin build/glic_codec_glitch_filter \
  --limit 50 --overwrite
```

Codecだけへ偏らない最終review用bankは、非codecの空間Metal family 14、原作スタイル
18、Codec Glitch 18を合わせた50 patternとして生成できます。各categoryへquotaを設け、
全動画を同じdry入力、解像度、frame数で再解析します。

```bash
python3 scripts/generate_mixed_glitch_selection.py input.mov \
  --output-dir test-videos/glitch-selection-50 \
  --codec-dir test-videos/codec-glitch --overwrite
```

```bash
python3 scripts/evaluate_codec_glitch_videos.py \
  --control control.mp4 \
  --candidate effect-a.mp4 --report effect-a.mp4.json --label effect-a \
  --candidate effect-b.mp4 --report effect-b.mp4.json --label effect-b \
  --output-json codec-ranking.json --output-md codec-ranking.md
```

C / Objective-C / Swiftからの組み込みは
[EMBEDDING.md](EMBEDDING.md#codec-glitch-c-api-macos-only)を参照してください。

## English

### Scope

Codec Glitch is a macOS-only asynchronous lane that encodes and decodes input
frames as H.264 with VideoToolbox. Depending on the effect, it modulates codec
quality, intentionally holds selected encoded frames, or composites decoded
history before or after the codec stages. It does not modify compressed H.264
VCL bytes.

It is not another compatibility level for an upstream GLIC preset:

| Path | Purpose | Presets / effects |
|---|---|---|
| File codec | Encode/decode images and `.glic` files | Upstream GLIC codec parameters |
| `original_visual` | Closer original-style realtime reconstruction | 37 audited presets, Strict / Fast Match |
| `compat_realtime` | Fast GPU composition and visual approximation for all 144 presets | GLIC presets plus 14 realtime families |
| `codec_glitch` | Video artifacts based on H.264 prediction, quantization, encoded-frame cadence, and decoded-frame history | 36 effects below; no GLIC preset semantics |

Codec Glitch does not translate `--preset`, `--preset-semantics`, or a search
`--canonical` recipe. It does not claim pixel equivalence with upstream GLIC.
Because it intentionally exercises platform codec behavior, fine details can
vary across macOS, Apple silicon, and VideoToolbox versions even with one seed.

### Pipeline

1. The host submits a BGRA `CVPixelBufferRef` to the asynchronous engine.
2. `codec_feedback` composites the prior codec-decoded result into the encode
   input, while `resolution_hop` scales its encode input down.
3. The selected VideoToolbox H.264 / HEVC / ProRes encoder produces a packet in `RealTime`
   mode. QP, bitrate, and generation effects strongly modulate or repeat codec
   stages.
4. `pframe_loss` and `idr_starvation` intentionally hold selected encoded
   frames and repeat the prior good decoded result.
5. All other H.264 packets reach the VideoToolbox hardware decoder without
   byte modification.
6. `slice_dropout`, `slice_transplant`, `payload_xor`, `chroma_codec_echo`,
   the six research effects, and scaled/pixelated recovery use
   Metal-compatible pixel-buffer pools and a
   Metal-backed CoreImage path. `reference_timewarp` selects an older frame
   from decoded `CVPixelBuffer` history configured from four to twelve frames.
7. The completed frame is delivered through a callback or bounded poll queue.
   Separate flags identify intentional holds and failure fallback.

H.264 / HEVC / ProRes encode/decode is provided by VideoToolbox, not implemented as a Metal
shader. Metal backs the native pixel-buffer/post-processing side of the lane.
`prepare` creates the queues and pools, prepares the normal-stage hardware
encoder, and validates the backend. Specialized QP, cascade, and downscale
encoders are created on first use; the decoder is created from the first
encoded sample. Startup does not reserve every stage.

VideoToolbox `RealTime` is always enabled. Low-latency rate control is also
enabled by default, and the default average bitrate is 4,000,000 bps. The
dynamic bitrate floor is
`min(averageBitRate, width * height * fps / 4)`, so it never raises bitrate
above the host configuration while preventing aggressive changes from making
the hardware encoder drop frames.

### Effects

| Effect | Primary operation |
|---|---|
| `qp_pump` | Temporally modulate per-frame QP across a wide range |
| `bitrate_crush` | Lower bitrate to the safe floor, then add Metal-backed compression patterns so damage remains visible without invalid output |
| `slice_dropout` | Replace selected horizontal slice rows in the current codec-decoded frame with the prior decoded frame to create row dropout/hold; VCL payloads are not modified |
| `slice_transplant` | Composite selected, position-shifted horizontal bands from codec-decoded history over the current decoded frame with Metal-backed CoreImage; VCL payloads are not modified |
| `pframe_loss` | Intentionally hold selected encoded non-IDR frames and repeat the prior decoded result |
| `idr_starvation` | Intentionally hold selected encoded IDR frames to reduce clean-refresh cadence while preserving watchdog recovery authority |
| `payload_xor` | After a clean decode, use Metal-backed CoreImage for posterization, deterministic RGB-channel rewiring, and displaced macroblock-like tiles; the name describes digital-damage styling, not payload-byte XOR |
| `reference_timewarp` | Deterministically select an older frame from decoded `CVPixelBuffer` history configured from four to twelve frames; compressed P packets are not reused |
| `codec_feedback` | Mix a previous decoded frame into the next encode input |
| `generation_cascade` | Run two or three encode/decode generations with stronger bitrate pressure and Metal-backed compression patterns in later generations |
| `resolution_hop` | Route through a one-half or one-quarter-resolution codec stage, then add pixelation while restoring full size |
| `chroma_codec_echo` | Mix delayed decoded color information into the current frame |
| `temporal_polyphony` | Use current-frame luminance structure as a region mask that selects between near and far decoded history, placing multiple temporal memories in one frame |
| `intra_cannibalism` | Recursively copy decoded blocks within one frame so later copies can consume earlier copies |
| `residual_rift` | Derive a current-minus-previous residual and add it back to a spatially warped prediction frame |
| `codec_grain_synth` | Generate a Metal-backed random field whose strength, temporal modulation, and grain size follow amount, rate, and feedback |
| `recursive_codec_skin` | Feed a noise-reduced, sharpened, color-adjusted prior output into the current frame to form a restoration-like recursive surface |
| `concealment_choreography` | Restore near or far decoded history only inside moving regions, creating regional concealment without dropping packets |
| `dual_codec_crossbreed` | Regionally combine compression reconstruction and delayed chroma history |
| `codec_pingpong` | Alternate between near and far codec-decoded history |
| `gop_accordion` | Stretch and compress bursts of history hold and codec damage |
| `bframe_braid` | Braid near and far display history across regions |
| `plane_split_codec` | Recombine current luminance with strongly delayed chroma |
| `roi_quality_islands` | Restore different codec-history quality inside moving regions |
| `codec_phase_mosaic` | Combine recursive block copy and restoration history |
| `encoder_hot_swap` | Cycle compression, chroma-history, and grain reconstruction |
| `pts_rubberband` | Alternate strong and weak history mixing for stalls and catch-up |
| `bitrate_raster` | Apply compression reconstruction over scanning history bands |
| `plane_time_split` | Recombine luminance and chroma from different decoded times |
| `reference_atlas` | Place decoded-history regions into a reference-frame atlas |
| `flow_lattice` | Warp temporal history with a deterministic lattice displacement |
| `scan_order_fold` | Fold history bands using scan order as a visual model |
| `regional_gop_clock` | Select decoded history with a different clock per region |
| `entropy_feedback` | Map local complexity to decoded-history feedback |
| `rolling_time_shutter` | Select a different past frame across the scan position |
| `asymmetric_plane_codec` | Reconstruct luminance and chroma with asymmetric scale and time |

The final six effects are realtime adaptations of research concepts that use
codec prediction as an artistic medium, constrained to the safe public
VideoToolbox API. They need no semantic-segmentation model, external depth,
optical flow, or audio FFT; current-frame luminance, deterministic region
masks, and decoded history provide the control signals. `intra_cannibalism`
does not edit IntraBC vectors, `residual_rift` does not edit compressed residual
coefficients, and `recursive_codec_skin` does not modify AV1 CDEF/restoration
syntax. Optical Flow Wound is not included because its proposed AV2 motion
refinement has no equivalent hook in the public H.264 VideoToolbox API. The
following ten Realtime Crossbreed effects use one selected hardware codec plus
decoded history; they do not claim two simultaneous hardware codec sessions.
The offline reconstruction and true multi-codec workflows are documented in
[CODEC_LAB.md](CODEC_LAB.md). The final eight effects also use decoded history
and CoreImage/Metal reconstruction. Their plane, flow, scan, and entropy names
are visual models, not claims of native compressed-field hooks.

Normalized `amount`, `rate`, and `feedback` controls range from 0 to 1. The C
API also exposes effect-specific QP, bitrate, generation-count, and
reduced-resolution controls. Values are clamped to engine-safe ranges.

### Safety and recovery boundary

- There is no API for ingesting or changing an external H.264 bitstream. H.264
  packets produced by VideoToolbox also reach the decoder with their VCL bytes
  unchanged for every effect.
- `slice_dropout`, `slice_transplant`, `payload_xor`, `reference_timewarp`, and
  the six research effects, ten Realtime Crossbreed effects, and final eight
  decoded-history effects act only on clean-decoded `CVPixelBuffer` output or its
  bounded history. They do not remove or transplant VCL slices, XOR payload
  bytes, or resubmit an older compressed P packet.
- Only `pframe_loss` and `idr_starvation` intentionally hold a selected encoded
  frame. Their output sets C++ `intentionalRepeat` or C ABI
  `intentional_repeat_frame`, together with `repeatedPreviousFrame` /
  `repeated_previous_frame`.
- An unexpected codec failure repeats the last good frame as fallback. Before
  the first successful decode, it emits the retained full-size input instead.
  Both set `non_intentional_fallback_frame=true`, separating them from intended
  temporal holds.
- A watchdog responds to actual consecutive failures by forcing the next frame
  to IDR through the existing session; it does not tear the session down. Only
  an explicit `glic_codec_glitch_reset()` drains and rebuilds codec sessions
  while clearing decoded history.
- Submit, callback-output, and poll-output queues are bounded. Backpressure drops work rather than
  allowing latency to grow without limit.
- C++ `packetWasModified` and the ABI-compatible `packet_was_modified` field
  remain false in this safe implementation. The existing
  `intentional_packet_drops` field name counts intentionally held encoded
  frames.
- `glic_codec_glitch_stats` and the video JSON report expose intentional
  repeats, non-intentional fallback, codec errors, watchdog recoveries,
  hardware-codec state, and latency. `codec_errors` covers encode, sample
  extraction, decode, and timeout failures rather than decoder failures alone.
  The ABI-compatible `poll_queue_drops` counter combines drops from either the
  callback or polling delivery queue.

This is an in-application visual effect for a self-produced stream, not a
general bitstream editor for third-party files, stored H.264, or network media.

### Performance contract

The design and measurement target is 960×540 at 30 fps, or a 33.33 ms frame
budget. The hard floor for a live system sharing the machine with other work is
20 fps with p95 at or below 50 ms. Multi-generation effects such as
`generation_cascade` can be more expensive than a single encode/decode pass.
Only a new stage/session uses warm-up deadlines of 500 ms for encode and 300 ms
for decode; sustained processing returns to 100 ms and 45 ms respectively.
Those outputs set `codec_warmup_frame=true` and are excluded from the filter's
sustained latency percentiles.

These are certification gates, not a universal performance guarantee. The
filter sets `reliability_passed=true` only when non-intentional fallback,
codec errors, watchdog recoveries, backpressure drops, and output-queue drops
are all zero. Intended holds are counted separately in
`intentional_repeat_frames` and do not fail that reliability gate. A realtime
20 fps flag additionally requires hardware encode/decode, at least 960×540,
at least 120 frames, processing and observed stream rates of at least 20 fps,
and p95 latency at or below 50 ms. `process_video.py` also rejects output whose
encoded/muxed frame count is unavailable or differs from the filter count. The
30 fps flag raises the rate gate to 30 fps and p95 gate to 33.334 ms.
Re-measure the intended effects and controls on the deployment Mac with
representative video.

On 2026-07-23, the six research effects were measured on an Apple M5 Max with
a 5.53-second, 166-frame live-action input converted to 960×540 at 30 fps. All
six preserved frame count, used hardware encode/decode, reported zero
unintended fallback and codec errors, and passed the 20 fps hard gate.

| Effect | End-to-end fps | Codec p95 |
|---|---:|---:|
| `temporal_polyphony` | 50.02 | 8.25 ms |
| `intra_cannibalism` | 49.82 | 12.36 ms |
| `residual_rift` | 60.34 | 7.84 ms |
| `codec_grain_synth` | 61.05 | 6.65 ms |
| `recursive_codec_skin` | 60.06 | 11.88 ms |
| `concealment_choreography` | 58.30 | 7.43 ms |

### Process a video

```bash
cmake -S . -B build -DCMAKE_BUILD_TYPE=Release
cmake --build build --target glic_codec_glitch_filter --parallel

python3 scripts/process_video.py input.mov output-codec.mp4 \
  --processing-mode codec_glitch \
  --codec-effect slice_transplant \
  --codec-amount 0.52 --codec-rate 0.34 --codec-feedback 0.58 \
  --seed 0x474c4943 \
  --width 960 --height 540 --fps 30 \
  --report output-codec.json --overwrite
```

All 36 names in the table are accepted by `--codec-effect`. Codec mode
requires macOS and VideoToolbox hardware codec support and rejects
`--backend cpu`.

Rank multiple rendered effects against an unchanged control with technical
dry/wet, temporal, reliability, and max-min fingerprint-diversity analysis:

```bash
python3 scripts/evaluate_codec_glitch_videos.py \
  --control control.mp4 \
  --candidate effect-a.mp4 --report effect-a.mp4.json --label effect-a \
  --candidate effect-b.mp4 --report effect-b.mp4.json --label effect-b \
  --output-json codec-ranking.json --output-md codec-ranking.md
```

For native integration, continue with
[EMBEDDING.md](EMBEDDING.md#codec-glitch-c-api-macos-only).

# Native Compressed Syntax Glitch

[日本語](#日本語) | [English](#english)

## 日本語

`process_native_syntax_glitch.py`は、復号画像へ似た処理をかけるproxyではありません。
FFglitch 0.10.2の`ffedit`を使い、MPEG-2/AVIの圧縮motion vector、量子化DCT係数
（`q_dct`）、quantizer scale（`qscale`）、またはMPEG-4 Part 2/AVIの圧縮motion
vectorをexportし、値を変更してentropy syntaxへtransplicateします。変更前後の
bitstream、syntax JSON、SHA-256、probe、全log、救済decode、閲覧用MP4を保持します。
HEVCではpinned x265 4.2へ小さなGPL hookを適用し、最終bitstreamのCABACを出力する
直前だけMVDまたは量子化transform coefficientを差し替えます。encoder内部の
reconstructionは変更前syntaxを使うため、decoder側に意図的な予測driftが生じます。
通常のx265 4.2しかない場合、MV 4種だけは`analysis-save/load`経路へfail-safe
fallbackできます。
H.264ではpinned x264のCABAC/CAVLC最終entropy出力へ同じ8効果を注入します。
さらに既存HEVC入力は、pinned FFmpeg 8.0.1のCABAC parserが復号した実MVD／量子化
係数をmotion compensation／逆量子化の直前で差し替えられます。この`preserve`
経路はsourceを再encode・変更しません。出力は変異したdecoder reconstructionと
閲覧用MP4であり、変異HEVC bitstreamではありません。
処理はofflineでありrealtimeを主張しません。

### 対応effect

| Feature | Codec | Effect |
|---|---|---|
| `mv` / H.264 MVD / HEVC MVD | MPEG-2 / MPEG-4 Part 2 / H.264 / HEVC | `compressed_motion_vector_vortex` |
| `mv` / H.264 MVD / HEVC MVD | MPEG-2 / MPEG-4 Part 2 / H.264 / HEVC | `compressed_motion_vector_mirror` |
| `mv` / H.264 MVD / HEVC MVD | MPEG-2 / MPEG-4 Part 2 / H.264 / HEVC | `compressed_motion_vector_quantizer` |
| `mv` / H.264 MVD / HEVC MVD | MPEG-2 / MPEG-4 Part 2 / H.264 / HEVC | `compressed_motion_vector_freeze` |
| `q_dct` / H.264 coefficient / HEVC coefficient | MPEG-2 / H.264 / HEVC | `compressed_coefficient_sign_flip` |
| `q_dct` / H.264 coefficient / HEVC coefficient | MPEG-2 / H.264 / HEVC | `compressed_coefficient_band_gate` |
| `q_dct` / H.264 coefficient / HEVC coefficient | MPEG-2 / H.264 / HEVC | `compressed_coefficient_transplant` |
| `q_dct` / H.264 coefficient / HEVC coefficient | MPEG-2 / H.264 / HEVC | `compressed_coefficient_scan_fold` |
| `qscale` | MPEG-2 | `compressed_quantizer_checkerboard` |
| `qscale` | MPEG-2 | `compressed_quantizer_wave` |
| `qscale` | MPEG-2 | `compressed_quantizer_raster` |
| `qscale` | MPEG-2 | `compressed_quantizer_pulse` |

### FFglitchの導入

FFglitchは本repositoryへ同梱しません。独立したGPL-2.0-or-later toolとして
subprocess実行します。Apple Silicon Macでは、公式archiveをchecksum検証して
`.cache/`へ導入できます。

```bash
FFEDIT="$(python3 scripts/install_ffglitch_reference.py --print-ffedit)"
export GLIC_FFEDIT="$FFEDIT"
```

別のOSでは[FFglitch公式Download](https://ffglitch.org/download/)から
`ffedit`を導入し、`--ffedit /absolute/path/to/ffedit`を指定してください。
HEVC late-entropy laneは、公式x265 commitをcloneし、別のGPL executableを
`.cache/`へbuildします。GLIC Metal libraryやSDKへbinaryを同梱・linkしません。

```bash
X265_GLIC="$(python3 scripts/build_x265_glitch_reference.py --print-x265)"
export GLIC_X265_HOOK="$X265_GLIC"
```

builderはcommit、patch/header hash、生成binary hashをsidecarへ保存します。
`process_native_syntax_glitch.py`はsidecarとbinary hashが一致した場合だけ
late-entropy hookを許可します。

H.264 CABAC/CAVLCと既存HEVC decoder hookも、公式sourceの固定commitから独立
CLIをbuildします。SDKにはsource/patch/builderだけを収録し、生成binaryを
同梱・linkしません。

```bash
X264_GLIC="$(python3 scripts/build_x264_glitch_reference.py --print-x264)"
FFMPEG_HEVC_GLIC="$(
  python3 scripts/build_ffmpeg_hevc_glitch_reference.py --print-ffmpeg
)"
export GLIC_X264_HOOK="$X264_GLIC"
export GLIC_FFMPEG_HEVC_DECODER_HOOK="$FFMPEG_HEVC_GLIC"
```

### 任意の入力を処理

`normalize`は入力を作業用MPEG-2またはMPEG-4 Part 2/AVIへencodeした後、その圧縮
syntaxを直接変更します。
FFglitch 0.10.2が短い未完結B-frame GOPでabortする境界を避けるため、この作業用
streamはI/P-frameだけで生成します。`preserve`では入力bitstreamを変更しません。

```bash
python3 scripts/process_native_syntax_glitch.py input.mov output.mp4 \
  --codec mpeg2 \
  --effect compressed_motion_vector_vortex \
  --amount 0.68 \
  --work-dir output.native-syntax-stages \
  --report output.json
```

既存のMPEG-2/AVIをpre-encodeせず直接変更する場合:

```bash
python3 scripts/process_native_syntax_glitch.py source.avi output.mp4 \
  --codec mpeg2 \
  --source-mode preserve \
  --effect compressed_coefficient_sign_flip \
  --amount 1.0
```

HEVCをx265 native encoder hookで処理する場合:

```bash
python3 scripts/process_native_syntax_glitch.py input.mov output-hevc.mp4 \
  --codec hevc \
  --effect compressed_coefficient_sign_flip \
  --amount 1.0 \
  --x265 "$X265_GLIC" \
  --hevc-hook entropy
```

`--hevc-hook auto`はverified custom binaryがあればlate-entropyを選び、通常の
x265 4.2ではMV effectだけをanalysis-save/loadへfallbackします。

H.264 CABACまたはCAVLCを処理する場合:

```bash
python3 scripts/process_native_syntax_glitch.py input.mov output-h264.mp4 \
  --codec h264 --h264-entropy cavlc \
  --effect compressed_motion_vector_mirror \
  --x264 "$X264_GLIC"
```

既存HEVCをsource再encodeなしでdecoder-side処理する場合:

```bash
python3 scripts/process_native_syntax_glitch.py existing-hevc.mov output.mp4 \
  --codec hevc --source-mode preserve \
  --effect compressed_coefficient_scan_fold \
  --ffmpeg-hevc-decoder "$FFMPEG_HEVC_GLIC"
```

この最後の経路はAnnex Bへstream-copyしたsourceを変更せずに読みます。
`output.mp4`は変異decoder reconstructionです。reportの
`output_is_mutated_hevc_bitstream: false`を必ず維持してください。

成功reportでは次を全て確認してください。

- `compressed_domain_edit: true`
- MPEG transplicationでは`decoded_pixels_modified_before_transplication: false`
- HEVC hookでは`decoded_pixels_modified_before_entropy_coding: false`
- 既存HEVC decoder hookでは`source_reencoded: false`、
  `source_bitstream_modified: false`、`output_is_mutated_hevc_bitstream: false`
- `mutation_evidence.changed_values > 0`
- encoder/transplication laneでは
  `source_bitstream.sha256 != damaged_bitstream.sha256`
- decoder laneではclean/mutated reconstructionのSHA-256が異なる
- `implementation_level`が`native_mpeg2_ffglitch_*`または
  `native_h264_x264_*`、`native_hevc_x265_*`、
  `native_hevc_ffmpeg_decoder_*`
- `qualified_preview: true`

実動画スモークではMPEG-2の12 effectとMPEG-4 Part 2の4 MV effectが24/24
frameを復号し、16/16 codec-effect variantで変更値とbitstream hash変化を
確認しました。代表的なMV vortexとqDCT sign flipは45/45 frame、
差分判定`VISIBLE`、video-render-qaのdecode/motion/exposure/color/complexity/
lightingをPASSし、repeated/frozen pairは0でした。
HEVC late-entropyの8 variantは実動画で全て24/24 frameを復号し、失敗0、
bitstream hash差、hook logの変更値を確認しました。8/8が`STRONG`で、control差の
MAEは26.61–50.60、10階調以上の変化は57.4–96.9%、luma SSIMは
0.4507–0.7158でした。通常x265のanalysis fallbackも24/24 frameを復号し、
mirrorは`SUBTLE`でした。
2026-07-26の追加検証では、H.264の8効果をCABAC/CAVLC双方へ適用した16/16
variantが24/24 frameを復号し、
全候補が差分評価を通過しました。既存HEVC decoder hookも8/8 variantが24/24
frameを出力し、全候補でparsed syntax変更値、clean reconstructionとのSHA差、
実動画差分を確認しました。

### 一括生成と非類似ranking

`evaluate_native_syntax_glitches.py`はLLM/APIを使わず、対応する全variantを
同一入力へ適用します。実動画のMAE、changed ratio、SSIM、edge差、時間差、
decode生存率を測り、視覚品質72%と既選択候補からの距離28%で決定的rankingを
生成します。既定ではMPEG-2 12、MPEG-4 Part 2 4、H.264 CABAC/CAVLC 16、
HEVC encoder/decoder各8の全48 variantです。`--resume`で
完了候補を再利用できます。

```bash
python3 scripts/evaluate_native_syntax_glitches.py input.mov \
  --output-dir search-runs/native-syntax \
  --codec all --amounts 0.65,0.85 --resume
```

出力は`ranking.json`、`ranking.md`、codec別difference report、各preview、
圧縮syntax/bitstream証跡です。

### H.264 / HEVCの境界

H.264/HEVC encoder hookはsourceを再encodeし、最終entropy syntaxだけを差し替え
ます。既存HEVC decoder hookはsourceを再encodeせず、CABACから復号された実値を
使いますが、変異bitstreamは生成しません。どちらも生VCL byte flipをMVD／係数
編集とは表示しません。既存`motion_vector_*`、`residual_*` effectは、引き続き
明記されたdecoded reconstruction proxyです。

## English

`process_native_syntax_glitch.py` is a real compressed-domain path, not a
decoded-pixel imitation. It uses FFglitch 0.10.2 `ffedit` to export MPEG-2
motion vectors, quantized DCT coefficients, or quantizer scales, and MPEG-4
Part 2 motion vectors. It mutates those encoded values and transplicates them
back into the entropy syntax. The source and changed bitstreams, original and
changed syntax JSON, hashes, probes, process logs, salvage decode, and review
MP4 remain available as evidence.

For HEVC, a small GPL hook is applied to pinned x265 4.2. It replaces MVD or
quantized transform coefficients only when the final CABAC bitstream is being
written. Encoder reconstruction retains the original syntax, intentionally
creating decoder-side prediction drift. Stock x265 4.2 remains a fail-safe
analysis-save/load fallback for the four motion effects.
H.264 uses a separately built pinned x264 hook at the final CABAC or CAVLC
entropy-writing boundary for the same four MVD and four coefficient effects.
An additional pinned FFmpeg 8.0.1 decoder hook accepts an existing HEVC
bitstream without source re-encoding and changes parsed MVD/coefficient values
before motion compensation or inverse quantization. It emits a mutated decoder
reconstruction, not a mutated HEVC bitstream.

Install the checksum-pinned Apple Silicon reference build with
`install_ffglitch_reference.py`, or provide an independently installed
`ffedit` through `GLIC_FFEDIT` / `--ffedit`. FFglitch is not bundled with GLIC
Metal; it runs as a separate GPL-2.0-or-later executable.

The default `normalize` mode accepts a general video and first makes an
FFglitch-compatible MPEG-2/AVI or MPEG-4 Part 2/AVI source whose syntax is then
edited. It uses I/P frames to avoid an FFglitch 0.10.2 abort on short streams
ending inside a B-frame GOP. Use `--source-mode preserve` to edit a compatible
AVI without that pre-encode. H.264 and the x265 HEVC encoder lane use normalized
Y4M input. HEVC `preserve` uses the decoder hook. Build the
external hook CLI with `build_x265_glitch_reference.py`; the processor verifies
its commit and binary hash sidecar before enabling late-entropy mode. This lane
is offline and makes no realtime claim.

Build all three pinned hook executables with:

```bash
X264_GLIC="$(python3 scripts/build_x264_glitch_reference.py --print-x264)"
X265_GLIC="$(python3 scripts/build_x265_glitch_reference.py --print-x265)"
FFMPEG_HEVC_GLIC="$(
  python3 scripts/build_ffmpeg_hevc_glitch_reference.py --print-ffmpeg
)"
```

Use `--codec h264 --h264-entropy cabac|cavlc --x264 "$X264_GLIC"` for
the H.264 encoder lane. Use `--codec hevc --source-mode preserve
--ffmpeg-hevc-decoder "$FFMPEG_HEVC_GLIC"` for an existing HEVC source.
The latter report must retain `source_reencoded: false`,
`source_bitstream_modified: false`, and
`output_is_mutated_hevc_bitstream: false`.

Actual-video smoke testing retained 24/24 frames and changed the bitstream hash
for all 16 FFglitch codec-effect variants: 12 MPEG-2 effects and four MPEG-4
Part 2 MV effects. Representative MV-vortex and qDCT-sign-flip outputs retained 45/45
frames, were both classified `VISIBLE`, and passed decode, motion, exposure,
color, complexity, and lighting QA with no repeated or frozen pairs.
All eight HEVC late-entropy variants decoded 24/24 frames with zero failed
runs, changed the bitstream hash, and emitted non-zero hook evidence. All
eight were `STRONG`: control MAE ranged from 26.61 to 50.60, pixels changing
by at least 10 levels from 57.4% to 96.9%, and luma SSIM from 0.4507 to
0.7158. The stock-x265 analysis fallback also retained 24/24 frames and was
`SUBTLE` for mirror.
On 2026-07-26, all 16 H.264 variants (eight effects in both CABAC and CAVLC)
retained 24/24
frames and passed actual-video difference checks. All eight existing-HEVC
decoder-hook variants also retained 24/24 frames, reported non-zero parsed
syntax changes, and differed from the clean decoder reconstruction.

The token-free batch evaluator renders every supported variant, measures
actual-video difference and decode survival, and produces a deterministic
quality/diversity ranking across 48 default variants: 12 MPEG-2, four MPEG-4
Part 2, 16 H.264 CABAC/CAVLC, eight HEVC encoder-hook, and eight HEVC
decoder-hook candidates. It supports resumable searches and retains every preview
and compressed-syntax evidence file.

The H.264 and HEVC encoder hooks re-encode their normalized source and then
change final entropy syntax. The existing-HEVC decoder hook does not re-encode
or modify the source bitstream, but it also does not output a mutated HEVC
bitstream. Raw VCL byte corruption is never reported as motion-vector or
coefficient editing, and older H.264/HEVC motion/residual effects remain
explicitly labeled decoded reconstruction proxies.

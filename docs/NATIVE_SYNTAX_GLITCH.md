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
処理はofflineでありrealtimeを主張しません。

### 対応effect

| Feature | Codec | Effect |
|---|---|---|
| `mv` / `hevc_cabac_mvd` | MPEG-2 / MPEG-4 Part 2 / HEVC | `compressed_motion_vector_vortex` |
| `mv` / `hevc_cabac_mvd` | MPEG-2 / MPEG-4 Part 2 / HEVC | `compressed_motion_vector_mirror` |
| `mv` / `hevc_cabac_mvd` | MPEG-2 / MPEG-4 Part 2 / HEVC | `compressed_motion_vector_quantizer` |
| `mv` / `hevc_cabac_mvd` | MPEG-2 / MPEG-4 Part 2 / HEVC | `compressed_motion_vector_freeze` |
| `q_dct` / `hevc_cabac_quantized_coefficient` | MPEG-2 / HEVC | `compressed_coefficient_sign_flip` |
| `q_dct` / `hevc_cabac_quantized_coefficient` | MPEG-2 / HEVC | `compressed_coefficient_band_gate` |
| `q_dct` / `hevc_cabac_quantized_coefficient` | MPEG-2 / HEVC | `compressed_coefficient_transplant` |
| `q_dct` / `hevc_cabac_quantized_coefficient` | MPEG-2 / HEVC | `compressed_coefficient_scan_fold` |
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
`--source-mode preserve`は、既存HEVCのCABACを直接書き戻すと誤解させないため
受け付けません。

成功reportでは次を全て確認してください。

- `compressed_domain_edit: true`
- MPEG transplicationでは`decoded_pixels_modified_before_transplication: false`
- HEVC hookでは`decoded_pixels_modified_before_entropy_coding: false`
- `mutation_evidence.changed_values > 0`
- `source_bitstream.sha256 != damaged_bitstream.sha256`
- `implementation_level`が`native_mpeg2_ffglitch_*`または
  `native_hevc_x265_cabac_*_late_entropy_injection`
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

### 一括生成と非類似ranking

`evaluate_native_syntax_glitches.py`はLLM/APIを使わず、対応する全variantを
同一入力へ適用します。実動画のMAE、changed ratio、SSIM、edge差、時間差、
decode生存率を測り、視覚品質72%と既選択候補からの距離28%で決定的rankingを
生成します。custom x265利用時は全24 codec-effect variantです。`--resume`で
完了候補を再利用できます。

```bash
python3 scripts/evaluate_native_syntax_glitches.py input.mov \
  --output-dir search-runs/native-syntax \
  --codec all --amounts 0.65,0.85 --resume
```

出力は`ranking.json`、`ranking.md`、codec別difference report、各preview、
圧縮syntax/bitstream証跡です。

### H.264 / HEVCの境界

H.264のCAVLC/CABAC MV・係数編集は未実装で、`--codec h264`はfail-closedします。
HEVCはMVD 4種と量子化係数4種をlate-entropy hookで実装しましたが、既存HEVC
bitstreamのCABACをtransplicateする経路ではなくsourceを再encodeします。生のVCL
byte flipを直接編集と表示しません。既存`motion_vector_*`、`residual_*` effectは、
引き続き明記されたdecoded reconstruction proxyです。

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

Install the checksum-pinned Apple Silicon reference build with
`install_ffglitch_reference.py`, or provide an independently installed
`ffedit` through `GLIC_FFEDIT` / `--ffedit`. FFglitch is not bundled with GLIC
Metal; it runs as a separate GPL-2.0-or-later executable.

The default `normalize` mode accepts a general video and first makes an
FFglitch-compatible MPEG-2/AVI or MPEG-4 Part 2/AVI source whose syntax is then
edited. It uses I/P frames to avoid an FFglitch 0.10.2 abort on short streams
ending inside a B-frame GOP. Use `--source-mode preserve` to edit a compatible
AVI without that pre-encode. HEVC always uses normalized Y4M input. Build the
external hook CLI with `build_x265_glitch_reference.py`; the processor verifies
its commit and binary hash sidecar before enabling late-entropy mode. This lane
is offline and makes no realtime claim.

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

The token-free batch evaluator renders every supported variant, measures
actual-video difference and decode survival, and produces a deterministic
quality/diversity ranking across 24 codec-effect variants when the custom x265
hook is available. It supports resumable searches and retains every preview
and compressed-syntax evidence file.

H.264 CAVLC/CABAC MV and coefficient editing remains unimplemented and fails
closed. HEVC supports four MVD and four quantized-coefficient effects through
the pinned late-entropy encoder hook, but existing-bitstream CABAC
transplication remains unavailable. Raw VCL byte corruption is not reported
as motion-vector or coefficient editing, and older H.264/HEVC motion/residual
effects remain explicitly labeled decoded reconstruction proxies.

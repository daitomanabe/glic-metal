# Native Compressed Syntax Glitch

[日本語](#日本語) | [English](#english)

## 日本語

`process_native_syntax_glitch.py`は、復号画像へ似た処理をかけるproxyではありません。
FFglitch 0.10.2の`ffedit`を使い、MPEG-2/AVIの圧縮motion vector、量子化DCT係数
（`q_dct`）、quantizer scale（`qscale`）、またはMPEG-4 Part 2/AVIの圧縮motion
vectorをexportし、値を変更してentropy syntaxへtransplicateします。変更前後の
bitstream、syntax JSON、SHA-256、probe、全log、救済decode、閲覧用MP4を保持します。
処理はofflineでありrealtimeを主張しません。

### 対応effect

| Feature | Codec | Effect |
|---|---|---|
| `mv` | MPEG-2 / MPEG-4 Part 2 | `compressed_motion_vector_vortex` |
| `mv` | MPEG-2 / MPEG-4 Part 2 | `compressed_motion_vector_mirror` |
| `mv` | MPEG-2 / MPEG-4 Part 2 | `compressed_motion_vector_quantizer` |
| `mv` | MPEG-2 / MPEG-4 Part 2 | `compressed_motion_vector_freeze` |
| `q_dct` | MPEG-2 | `compressed_coefficient_sign_flip` |
| `q_dct` | MPEG-2 | `compressed_coefficient_band_gate` |
| `q_dct` | MPEG-2 | `compressed_coefficient_transplant` |
| `q_dct` | MPEG-2 | `compressed_coefficient_scan_fold` |
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

成功reportでは次を全て確認してください。

- `compressed_domain_edit: true`
- `decoded_pixels_modified_before_transplication: false`
- `mutation_evidence.changed_values > 0`
- `source_bitstream.sha256 != damaged_bitstream.sha256`
- `implementation_level`が`native_mpeg2_ffglitch_*_entropy_transplication`
- `qualified_preview: true`

実動画スモークではMPEG-2の12 effectとMPEG-4 Part 2の4 MV effectが24/24
frameを復号し、16/16 codec-effect variantで変更値とbitstream hash変化を
確認しました。代表的なMV vortexとqDCT sign flipは45/45 frame、
差分判定`VISIBLE`、video-render-qaのdecode/motion/exposure/color/complexity/
lightingをPASSし、repeated/frozen pairは0でした。

### 一括生成と非類似ranking

`evaluate_native_syntax_glitches.py`はLLM/APIを使わず、対応する全variantを
同一入力へ適用します。実動画のMAE、changed ratio、SSIM、edge差、時間差、
decode生存率を測り、視覚品質72%と既選択候補からの距離28%で決定的rankingを
生成します。`--resume`で完了候補を再利用できます。

```bash
python3 scripts/evaluate_native_syntax_glitches.py input.mov \
  --output-dir search-runs/native-syntax \
  --codec all --amounts 0.65,0.85 --resume
```

出力は`ranking.json`、`ranking.md`、codec別difference report、各preview、
圧縮syntax/bitstream証跡です。

### H.264 / HEVCの境界

このlaneはMPEG-2とMPEG-4 Part 2の対応featureだけを扱います。H.264の
CAVLC/CABAC、HEVCのCABAC内部へmotion vectorやtransform coefficientを安全に
再挿入するencoder hookは実装していません。`--codec h264`または`--codec hevc`は
fail-closedします。生のVCL byte flipを直接編集と表示しません。H.264/HEVCの
既存`motion_vector_*`、`residual_*` effectは、引き続き明記されたdecoded
reconstruction proxyです。

## English

`process_native_syntax_glitch.py` is a real compressed-domain path, not a
decoded-pixel imitation. It uses FFglitch 0.10.2 `ffedit` to export MPEG-2
motion vectors, quantized DCT coefficients, or quantizer scales, and MPEG-4
Part 2 motion vectors. It mutates those encoded values and transplicates them
back into the entropy syntax. The source and changed bitstreams, original and
changed syntax JSON, hashes, probes, process logs, salvage decode, and review
MP4 remain available as evidence.

Install the checksum-pinned Apple Silicon reference build with
`install_ffglitch_reference.py`, or provide an independently installed
`ffedit` through `GLIC_FFEDIT` / `--ffedit`. FFglitch is not bundled with GLIC
Metal; it runs as a separate GPL-2.0-or-later executable.

The default `normalize` mode accepts a general video and first makes an
FFglitch-compatible MPEG-2/AVI or MPEG-4 Part 2/AVI source whose syntax is then
edited. It uses I/P frames to avoid an FFglitch 0.10.2 abort on short streams
ending inside a B-frame GOP. Use `--source-mode preserve` to edit a compatible
AVI without that pre-encode. This lane is offline and makes no realtime claim.

Actual-video smoke testing retained 24/24 frames and changed the bitstream hash
for all 16 codec-effect variants: 12 MPEG-2 effects and four MPEG-4 Part 2 MV
effects. Representative MV-vortex and qDCT-sign-flip outputs retained 45/45
frames, were both classified `VISIBLE`, and passed decode, motion, exposure,
color, complexity, and lighting QA with no repeated or frozen pairs.

The token-free batch evaluator renders every supported variant, measures
actual-video difference and decode survival, and produces a deterministic
quality/diversity ranking. It supports resumable searches and retains every
preview and compressed-syntax evidence file.

H.264 CAVLC/CABAC and HEVC CABAC reinsertion are not implemented.
`--codec h264` and `--codec hevc` fail closed. Raw VCL byte corruption is not
reported as motion-vector or coefficient editing, and the older H.264/HEVC
motion/residual effects remain explicitly labeled decoded reconstruction
proxies.

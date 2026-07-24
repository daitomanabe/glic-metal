# Native Compressed Syntax Glitch

[日本語](#日本語) | [English](#english)

## 日本語

`process_native_syntax_glitch.py`は、復号画像へ似た処理をかけるproxyではありません。
FFglitch 0.10.2の`ffedit`を使い、MPEG-2/AVI bitstreamから圧縮motion vector
または量子化DCT係数（`q_dct`）をexportし、値を変更してentropy syntaxへ
transplicateします。変更前後のbitstream、syntax JSON、SHA-256、probe、全log、
救済decode、閲覧用MP4を保持します。処理はofflineでありrealtimeを主張しません。

### 対応effect

| Feature | Effect |
|---|---|
| `mv` | `compressed_motion_vector_vortex` |
| `mv` | `compressed_motion_vector_mirror` |
| `mv` | `compressed_motion_vector_quantizer` |
| `mv` | `compressed_motion_vector_freeze` |
| `q_dct` | `compressed_coefficient_sign_flip` |
| `q_dct` | `compressed_coefficient_band_gate` |
| `q_dct` | `compressed_coefficient_transplant` |
| `q_dct` | `compressed_coefficient_scan_fold` |

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

`normalize`は入力を作業用MPEG-2/AVIへencodeした後、その圧縮syntaxを直接変更します。
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

実動画スモークでは8/8 effectが24/24 frameを復号し、全てで変更値とbitstream
hash変化を確認しました。代表的なMV vortexとqDCT sign flipは45/45 frame、
差分判定`VISIBLE`、video-render-qaのdecode/motion/exposure/color/complexity/
lightingをPASSし、repeated/frozen pairは0でした。

### H.264 / HEVCの境界

このlaneはMPEG-2だけに対応します。H.264のCAVLC/CABAC、HEVCのCABAC内部へ
motion vectorやtransform coefficientを安全に再挿入するencoder hookは実装して
いません。`--codec h264`または`--codec hevc`はfail-closedします。生のVCL byte
flipを直接編集と表示しません。H.264/HEVCの既存`motion_vector_*`、
`residual_*` effectは、引き続き明記されたdecoded reconstruction proxyです。

## English

`process_native_syntax_glitch.py` is a real compressed-domain path, not a
decoded-pixel imitation. It uses FFglitch 0.10.2 `ffedit` to export MPEG-2
motion vectors or quantized DCT coefficients, mutates those encoded values,
and transplicates them back into the entropy syntax. The source and changed
bitstreams, original and changed syntax JSON, hashes, probes, process logs,
salvage decode, and review MP4 remain available as evidence.

Install the checksum-pinned Apple Silicon reference build with
`install_ffglitch_reference.py`, or provide an independently installed
`ffedit` through `GLIC_FFEDIT` / `--ffedit`. FFglitch is not bundled with GLIC
Metal; it runs as a separate GPL-2.0-or-later executable.

The default `normalize` mode accepts a general video and first makes the
FFglitch-compatible MPEG-2/AVI source whose syntax is then edited. It uses
I/P frames to avoid an FFglitch 0.10.2 abort on short streams ending inside a
B-frame GOP. Use `--source-mode preserve` to edit an existing MPEG-2/AVI
without that pre-encode. This lane is offline and makes no realtime claim.

Actual-video smoke testing retained 24/24 frames and changed the bitstream hash
for all eight effects. Representative MV-vortex and qDCT-sign-flip outputs
retained 45/45 frames, were both classified `VISIBLE`, and passed decode,
motion, exposure, color, complexity, and lighting QA with no repeated or frozen
pairs.

H.264 CAVLC/CABAC and HEVC CABAC reinsertion are not implemented.
`--codec h264` and `--codec hevc` fail closed. Raw VCL byte corruption is not
reported as motion-vector or coefficient editing, and the older H.264/HEVC
motion/residual effects remain explicitly labeled decoded reconstruction
proxies.

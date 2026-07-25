# Third-party notices

GLIC Metal is a port and extension of open-source work. The notices below are
included for source and binary distributions. Links are informational; the
license text shipped with this repository controls the included material.

## GlitchCodec/GLIC

The codec design, preset format, preset corpus, Processing-compatible behavior,
and portions of the port are derived from
[GlitchCodec/GLIC](https://github.com/GlitchCodec/GLIC), audited at commit
`460e61bf9b01f7415cf973b3d655a0ae2c7962a7`.

Copyright (c) 2017 GlitchCodec

Licensed under the MIT License. The complete license text is in the repository
root at [LICENSE](LICENSE).

This project is independently maintained and is not an official GlitchCodec
release.

## JWave

The original-visual CDF 9/7 implementation uses coefficients and behavioral
references from the JWave library bundled by the audited GLIC revision.

Copyright (c) 2008-2025 JWave Christian (graetz23@gmail.com)

Permission is hereby granted, free of charge, to any person obtaining a copy
of this software and associated documentation files (the "Software"), to deal
in the Software without restriction, including without limitation the rights
to use, copy, modify, merge, publish, distribute, sublicense, and/or sell
copies of the Software, and to permit persons to whom the Software is
furnished to do so, subject to the following conditions:

The above copyright notice and this permission notice shall be included in all
copies or substantial portions of the Software.

THE SOFTWARE IS PROVIDED "AS IS", WITHOUT WARRANTY OF ANY KIND, EXPRESS OR
IMPLIED, INCLUDING BUT NOT LIMITED TO THE WARRANTIES OF MERCHANTABILITY,
FITNESS FOR A PARTICULAR PURPOSE AND NONINFRINGEMENT. IN NO EVENT SHALL THE
AUTHORS OR COPYRIGHT HOLDERS BE LIABLE FOR ANY CLAIM, DAMAGES OR OTHER
LIABILITY, WHETHER IN AN ACTION OF CONTRACT, TORT OR OTHERWISE, ARISING FROM,
OUT OF OR IN CONNECTION WITH THE SOFTWARE OR THE USE OR OTHER DEALINGS IN THE
SOFTWARE.

Source and current license:
[graetz23/JWave](https://github.com/graetz23/JWave).

## stb_image and stb_image_write

Image I/O uses files from [nothings/stb](https://github.com/nothings/stb),
included as the `external/stb` Git submodule at commit
`31c1ad37456438565541f4919958214b6e762fb4`. This project selects the MIT option
offered by stb.

Copyright (c) 2017 Sean Barrett

Permission is hereby granted, free of charge, to any person obtaining a copy of
this software and associated documentation files (the "Software"), to deal in
the Software without restriction, including without limitation the rights to
use, copy, modify, merge, publish, distribute, sublicense, and/or sell copies
of the Software, and to permit persons to whom the Software is furnished to do
so, subject to the following conditions:

The above copyright notice and this permission notice shall be included in all
copies or substantial portions of the Software.

THE SOFTWARE IS PROVIDED "AS IS", WITHOUT WARRANTY OF ANY KIND, EXPRESS OR
IMPLIED, INCLUDING BUT NOT LIMITED TO THE WARRANTIES OF MERCHANTABILITY,
FITNESS FOR A PARTICULAR PURPOSE AND NONINFRINGEMENT. IN NO EVENT SHALL THE
AUTHORS OR COPYRIGHT HOLDERS BE LIABLE FOR ANY CLAIM, DAMAGES OR OTHER
LIABILITY, WHETHER IN AN ACTION OF CONTRACT, TORT OR OTHERWISE, ARISING FROM,
OUT OF OR IN CONNECTION WITH THE SOFTWARE OR THE USE OR OTHER DEALINGS IN THE
SOFTWARE.

The submodule also retains its complete upstream notice in
`external/stb/LICENSE`.

## FFglitch

The optional Native Compressed Syntax Glitch workflow invokes
[FFglitch](https://ffglitch.org/) `ffedit` 0.10.2 as a separate executable.
FFglitch is not included in this repository, the GLIC Metal library, or its
resource bundle. The reference installer downloads the official archive
directly from the FFglitch project and verifies its pinned SHA-256 checksum.

FFglitch Copyright (c) 2017-2024 Ramiro Polla and FFmpeg contributors.

The official binary reports GNU General Public License version 2 or, at the
user's option, any later version. Its archive includes the complete upstream
notice, and source is available from the
[official FFglitch download page](https://ffglitch.org/download/).

## x265

The optional HEVC late-entropy workflow builds
[x265](https://github.com/Multicorewareinc/x265) 4.2 from pinned commit
`e444744c03978c1fb4e037168967020cf2648427` and applies the separately
distributed GLIC MVD/coefficient hook. The resulting CLI is an external
GPL-2.0-or-later executable. It is cached locally and is not included in this
repository, linked into the MIT-licensed GLIC Metal library, or bundled in the
SDK.

x265 Copyright (c) 2013-2025 MulticoreWare, Inc. and x265 contributors.

The builder retains the complete upstream source and license in its cache.
The hook source carries the same GPL-2.0-or-later license because it is
compiled into that external executable.

## x264

The optional H.264 late-entropy workflow builds
[x264](https://code.videolan.org/videolan/x264) from pinned commit
`0480cb05fa188d37ae87e8f4fd8f1aea3711f7ee` and applies the separately
distributed GLIC CABAC/CAVLC MVD/coefficient hook. The resulting CLI is an
external GPL-2.0-or-later executable. It is cached locally and is not included
in this repository, linked into the MIT-licensed GLIC Metal library, or
bundled in the SDK.

x264 is Copyright (c) 2003-2025 x264 project contributors.

The builder retains the complete upstream source and license in its cache.
The hook source is GPL-2.0-or-later because it is compiled into that external
executable.

## FFmpeg HEVC decoder hook

The optional existing-HEVC workflow builds
[FFmpeg](https://github.com/FFmpeg/FFmpeg) 8.0.1 from pinned commit
`894da5ca7d742e4429ffb2af534fcda0103ef593` and applies the separately
distributed GLIC decoder-side MVD/coefficient hook. The minimal external CLI
uses FFmpeg's LGPL-2.1-or-later configuration. It is cached locally and is not
included in this repository, linked into the MIT-licensed GLIC Metal library,
or bundled in the SDK.

FFmpeg is Copyright (c) 2000-2025 the FFmpeg developers.

The builder retains the complete upstream source and license in its cache.
The hook changes parsed HEVC syntax during reconstruction; it does not modify
or emit an HEVC bitstream.

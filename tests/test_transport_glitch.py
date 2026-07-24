from __future__ import annotations

import importlib.util
from pathlib import Path
import sys


ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "scripts"))
from structured_bitstream import AnnexBUnit, join_annexb
from transport_glitch import (
    decode_rtp_capture,
    depacketize_h264_rtp,
    encode_rtp_capture,
    mutate_hls_playlist,
    mutate_mpegts_continuity,
    mutate_rtp_sequence_jitter,
    packetize_h264_rtp,
)


def load_runner():
    spec = importlib.util.spec_from_file_location(
        "process_transport_glitch",
        ROOT / "scripts" / "process_transport_glitch.py",
    )
    assert spec is not None and spec.loader is not None
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


def main() -> int:
    packets = []
    for index in range(24):
        packet = bytearray(188)
        packet[0] = 0x47
        packet[1] = 0x01
        packet[2] = 0x00
        packet[3] = 0x10 | (index & 0x0F)
        packets.append(bytes(packet))
    mutated, evidence = mutate_mpegts_continuity(
        b"".join(packets), 0.9, 17
    )
    assert len(mutated) == 188 * 24
    assert evidence["changed_continuity_counters"] > 0

    annexb = join_annexb(
        [
            AnnexBUnit(b"\x00\x00\x00\x01", bytearray((0x67,) + (1,) * 40)),
            AnnexBUnit(b"\x00\x00\x00\x01", bytearray((0x65,) + (2,) * 5000)),
            AnnexBUnit(b"\x00\x00\x00\x01", bytearray((0x41,) + (3,) * 400)),
        ]
    )
    source_rtp = packetize_h264_rtp(annexb, mtu=400)
    capture = encode_rtp_capture(source_rtp)
    assert len(decode_rtp_capture(capture)) == len(source_rtp)
    jittered, jitter_evidence = mutate_rtp_sequence_jitter(
        source_rtp, 0.8, 23
    )
    damaged, depacket_evidence = depacketize_h264_rtp(jittered)
    assert damaged.startswith(b"\x00\x00\x00\x01")
    assert jitter_evidence["selected_packets"] > 0
    assert depacket_evidence["output_nal_units"] > 0

    playlist = (
        "#EXTM3U\n#EXT-X-VERSION:3\n"
        "#EXTINF:0.5,\na.ts\n#EXTINF:0.5,\nb.ts\n"
        "#EXTINF:0.5,\nc.ts\n#EXT-X-ENDLIST\n"
    )
    changed, hls_evidence = mutate_hls_playlist(playlist, 0.7, 5)
    assert "#EXT-X-DISCONTINUITY" in changed
    assert hls_evidence["media_segments"] == 3

    runner = load_runner()
    assert runner.EFFECTS == (
        "mpegts_continuity_fracture",
        "rtp_sequence_jitter",
        "hls_segment_boundary_splice",
    )
    assert "not_network_capture" in runner.IMPLEMENTATION_LEVEL[
        "rtp_sequence_jitter"
    ]
    print("PASS transport glitch helpers and implementation labels")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())

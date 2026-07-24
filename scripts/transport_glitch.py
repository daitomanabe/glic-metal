"""Deterministic MPEG-TS, RTP packet-model, and HLS mutation helpers."""

from __future__ import annotations

from dataclasses import dataclass
from collections import Counter
import random
import struct

from structured_bitstream import AnnexBUnit, join_annexb, split_annexb


TS_PACKET_BYTES = 188
RTP_HEADER_BYTES = 12


def mutate_mpegts_continuity(
    data: bytes, amount: float, seed: int
) -> tuple[bytes, dict]:
    if not data or len(data) % TS_PACKET_BYTES:
        raise ValueError("MPEG-TS input is not aligned to 188-byte packets")
    packets = [
        bytearray(data[offset : offset + TS_PACKET_BYTES])
        for offset in range(0, len(data), TS_PACKET_BYTES)
    ]
    if any(packet[0] != 0x47 for packet in packets):
        raise ValueError("MPEG-TS sync byte is missing")
    rng = random.Random(seed)
    changed = 0
    candidate = 0
    pids: set[int] = set()
    payload_pids = Counter()
    for packet in packets:
        pid = ((packet[1] & 0x1F) << 8) | packet[2]
        adaptation_control = (packet[3] >> 4) & 0x03
        if pid > 0x1F and pid not in {0x1000, 0x1FFF} and adaptation_control in {1, 3}:
            payload_pids[pid] += 1
    if not payload_pids:
        raise RuntimeError("MPEG-TS stream has no eligible elementary PID")
    dominant_pid = payload_pids.most_common(1)[0][0]
    probability = 0.015 + amount * 0.075
    for packet_index, packet in enumerate(packets):
        pid = ((packet[1] & 0x1F) << 8) | packet[2]
        adaptation_control = (packet[3] >> 4) & 0x03
        payload_unit_start = bool(packet[1] & 0x40)
        if (
            pid != dominant_pid
            or adaptation_control not in {1, 3}
            or payload_unit_start
            or packet_index < 8
        ):
            continue
        candidate += 1
        pids.add(pid)
        if rng.random() > probability:
            continue
        original = packet[3] & 0x0F
        jump = 1 + rng.randrange(1, max(2, round(2 + amount * 7)))
        packet[3] = (packet[3] & 0xF0) | ((original + jump) & 0x0F)
        changed += 1
    if changed == 0:
        raise RuntimeError("no MPEG-TS continuity counters were changed")
    return b"".join(packets), {
        "packet_bytes": TS_PACKET_BYTES,
        "packet_count": len(packets),
        "candidate_packets": candidate,
        "changed_continuity_counters": changed,
        "dominant_elementary_pid": dominant_pid,
        "mutated_pids": sorted(pids),
    }


@dataclass
class RtpPacket:
    payload: bytes
    sequence: int
    timestamp: int
    marker: bool
    payload_type: int = 96
    ssrc: int = 0x474C4943

    def to_bytes(self) -> bytes:
        first = 0x80
        second = (0x80 if self.marker else 0) | (self.payload_type & 0x7F)
        return struct.pack(
            ">BBHII",
            first,
            second,
            self.sequence & 0xFFFF,
            self.timestamp & 0xFFFFFFFF,
            self.ssrc & 0xFFFFFFFF,
        ) + self.payload

    @classmethod
    def from_bytes(cls, data: bytes) -> "RtpPacket":
        if len(data) < RTP_HEADER_BYTES:
            raise ValueError("truncated RTP packet")
        first, second, sequence, timestamp, ssrc = struct.unpack(
            ">BBHII", data[:RTP_HEADER_BYTES]
        )
        if first >> 6 != 2:
            raise ValueError("RTP version is not 2")
        return cls(
            payload=data[RTP_HEADER_BYTES:],
            sequence=sequence,
            timestamp=timestamp,
            marker=bool(second & 0x80),
            payload_type=second & 0x7F,
            ssrc=ssrc,
        )


def packetize_h264_rtp(
    annexb: bytes,
    *,
    mtu: int = 1200,
    sequence: int = 0,
    timestamp_step: int = 3000,
) -> list[RtpPacket]:
    if mtu <= RTP_HEADER_BYTES + 3:
        raise ValueError("RTP MTU is too small")
    packets: list[RtpPacket] = []
    timestamp = 0
    max_payload = mtu - RTP_HEADER_BYTES
    for unit in split_annexb(annexb):
        nal = bytes(unit.payload)
        if not nal:
            continue
        if len(nal) <= max_payload:
            packets.append(RtpPacket(nal, sequence, timestamp, True))
            sequence = (sequence + 1) & 0xFFFF
        else:
            indicator = (nal[0] & 0xE0) | 28
            nal_type = nal[0] & 0x1F
            body = nal[1:]
            fragment_bytes = max_payload - 2
            for offset in range(0, len(body), fragment_bytes):
                fragment = body[offset : offset + fragment_bytes]
                start = offset == 0
                end = offset + len(fragment) >= len(body)
                header = nal_type | (0x80 if start else 0) | (0x40 if end else 0)
                packets.append(
                    RtpPacket(
                        bytes((indicator, header)) + fragment,
                        sequence,
                        timestamp,
                        end,
                    )
                )
                sequence = (sequence + 1) & 0xFFFF
        timestamp = (timestamp + timestamp_step) & 0xFFFFFFFF
    if not packets:
        raise RuntimeError("H.264 stream produced no RTP packets")
    return packets


def encode_rtp_capture(packets: list[RtpPacket]) -> bytes:
    chunks: list[bytes] = []
    for packet in packets:
        encoded = packet.to_bytes()
        if len(encoded) > 0xFFFF:
            raise ValueError("RTP packet exceeds capture record size")
        chunks.append(struct.pack(">H", len(encoded)) + encoded)
    return b"".join(chunks)


def decode_rtp_capture(data: bytes) -> list[RtpPacket]:
    packets: list[RtpPacket] = []
    offset = 0
    while offset < len(data):
        if offset + 2 > len(data):
            raise ValueError("truncated RTP capture length")
        size = struct.unpack(">H", data[offset : offset + 2])[0]
        offset += 2
        if size < RTP_HEADER_BYTES or offset + size > len(data):
            raise ValueError("truncated RTP capture record")
        packets.append(RtpPacket.from_bytes(data[offset : offset + size]))
        offset += size
    return packets


def mutate_rtp_sequence_jitter(
    packets: list[RtpPacket], amount: float, seed: int
) -> tuple[list[RtpPacket], dict]:
    if len(packets) < 4:
        raise RuntimeError("too few RTP packets for sequence jitter")
    rng = random.Random(seed)
    output = [
        RtpPacket(
            packet.payload,
            packet.sequence,
            packet.timestamp,
            packet.marker,
            packet.payload_type,
            packet.ssrc,
        )
        for packet in packets
    ]
    selected = 0
    dropped = 0
    reordered = 0
    index = 1
    probability = 0.01 + amount * 0.06
    while index < len(output) - 1:
        if rng.random() > probability:
            index += 1
            continue
        selected += 1
        packet = output[index]
        packet.sequence = (packet.sequence + rng.choice((-3, -2, 2, 3))) & 0xFFFF
        if not packet.marker and rng.random() < 0.08 + amount * 0.14:
            output.pop(index)
            dropped += 1
            continue
        output[index], output[index + 1] = output[index + 1], output[index]
        reordered += 1
        index += 2
    if selected == 0:
        fallback = min(len(output) - 2, max(1, len(output) // 2))
        output[fallback].sequence = (output[fallback].sequence + 2) & 0xFFFF
        output[fallback], output[fallback + 1] = (
            output[fallback + 1],
            output[fallback],
        )
        selected = 1
        reordered = 1
    return output, {
        "packet_model": "RFC6184_H264_offline_length_prefixed_capture",
        "network_capture": False,
        "source_packets": len(packets),
        "selected_packets": selected,
        "dropped_packets": dropped,
        "reordered_pairs": reordered,
        "output_packets": len(output),
    }


def depacketize_h264_rtp(packets: list[RtpPacket]) -> tuple[bytes, dict]:
    units: list[AnnexBUnit] = []
    fragments = bytearray()
    fragment_sequence: int | None = None
    discarded_fragments = 0
    completed_fragmented_units = 0
    for packet in packets:
        if not packet.payload:
            continue
        nal_type = packet.payload[0] & 0x1F
        if nal_type != 28:
            fragments.clear()
            fragment_sequence = None
            units.append(AnnexBUnit(b"\x00\x00\x00\x01", bytearray(packet.payload)))
            continue
        if len(packet.payload) < 2:
            discarded_fragments += 1
            continue
        indicator, header = packet.payload[:2]
        start = bool(header & 0x80)
        end = bool(header & 0x40)
        if start:
            fragments = bytearray(((indicator & 0xE0) | (header & 0x1F),))
            fragments.extend(packet.payload[2:])
            fragment_sequence = packet.sequence
            continue
        expected = (
            (fragment_sequence + 1) & 0xFFFF
            if fragment_sequence is not None
            else None
        )
        if expected is None or packet.sequence != expected or not fragments:
            fragments.clear()
            fragment_sequence = None
            discarded_fragments += 1
            continue
        fragments.extend(packet.payload[2:])
        fragment_sequence = packet.sequence
        if end:
            units.append(AnnexBUnit(b"\x00\x00\x00\x01", fragments))
            fragments = bytearray()
            fragment_sequence = None
            completed_fragmented_units += 1
    if not units:
        raise RuntimeError("RTP depacketization produced no H.264 NAL units")
    return join_annexb(units), {
        "output_nal_units": len(units),
        "completed_fragmented_units": completed_fragmented_units,
        "discarded_fragments": discarded_fragments,
    }


def mutate_hls_playlist(
    text: str, amount: float, seed: int
) -> tuple[str, dict]:
    lines = text.splitlines()
    entries: list[list[str]] = []
    prefix: list[str] = []
    footer: list[str] = []
    index = 0
    while index < len(lines):
        if lines[index].startswith("#EXTINF"):
            if index + 1 >= len(lines) or lines[index + 1].startswith("#"):
                raise ValueError("HLS EXTINF has no segment URI")
            entries.append([lines[index], lines[index + 1]])
            index += 2
            continue
        if entries:
            footer.append(lines[index])
        else:
            prefix.append(lines[index])
        index += 1
    if len(entries) < 2:
        raise RuntimeError("HLS playlist needs at least two media segments")
    rng = random.Random(seed)
    pair = min(len(entries) - 2, max(0, round(amount * (len(entries) - 2))))
    if len(entries) > 2:
        pair = (pair + rng.randrange(len(entries) - 1)) % (len(entries) - 1)
    entries[pair], entries[pair + 1] = entries[pair + 1], entries[pair]
    entries[pair].insert(0, "#EXT-X-DISCONTINUITY")
    output = prefix + [item for entry in entries for item in entry] + footer
    return "\n".join(output) + "\n", {
        "media_segments": len(entries),
        "reordered_segment_pair": [pair, pair + 1],
        "inserted_discontinuities": 1,
    }

#!/usr/bin/env python3
"""Serve local preview media with HTTP byte-range support."""

from __future__ import annotations

import os
import re
import sys
from http.server import SimpleHTTPRequestHandler, ThreadingHTTPServer


class _RangeFile:
    def __init__(self, handle, length: int) -> None:
        self._handle = handle
        self._remaining = length

    def read(self, size: int = -1) -> bytes:
        if self._remaining <= 0:
            return b""
        amount = self._remaining if size < 0 else min(size, self._remaining)
        data = self._handle.read(amount)
        self._remaining -= len(data)
        return data

    def close(self) -> None:
        self._handle.close()


class RangeRequestHandler(SimpleHTTPRequestHandler):
    range_pattern = re.compile(r"^bytes=(\d*)-(\d*)$")

    def send_head(self):
        path = self.translate_path(self.path)
        range_header = self.headers.get("Range")
        if range_header is None or not os.path.isfile(path):
            return super().send_head()

        file_size = os.path.getsize(path)
        match = self.range_pattern.fullmatch(range_header.strip())
        if match is None or file_size <= 0:
            self.send_error(416, "Invalid byte range")
            return None

        first, last = match.groups()
        if not first:
            suffix_length = int(last or "0")
            if suffix_length <= 0:
                self.send_error(416, "Invalid byte range")
                return None
            start = max(0, file_size - suffix_length)
            end = file_size - 1
        else:
            start = int(first)
            end = min(int(last), file_size - 1) if last else file_size - 1
        if start >= file_size or end < start:
            self.send_response(416)
            self.send_header("Content-Range", f"bytes */{file_size}")
            self.end_headers()
            return None

        length = end - start + 1
        handle = open(path, "rb")
        handle.seek(start)
        self.send_response(206)
        self.send_header("Content-Type", self.guess_type(path))
        self.send_header("Content-Length", str(length))
        self.send_header("Content-Range", f"bytes {start}-{end}/{file_size}")
        self.send_header("Accept-Ranges", "bytes")
        self.send_header("Access-Control-Allow-Origin", "*")
        self.end_headers()
        return _RangeFile(handle, length)

    def do_OPTIONS(self) -> None:
        self.send_response(204)
        self.send_header("Access-Control-Allow-Origin", "*")
        self.send_header("Access-Control-Allow-Headers", "Range")
        self.end_headers()


def main() -> int:
    port = int(sys.argv[1]) if len(sys.argv) > 1 else 8000
    server = ThreadingHTTPServer(("127.0.0.1", port), RangeRequestHandler)
    print(f"Range-enabled HTTP server on http://127.0.0.1:{port}", flush=True)
    try:
        server.serve_forever()
    except KeyboardInterrupt:
        pass
    finally:
        server.server_close()
    return 0


if __name__ == "__main__":
    raise SystemExit(main())

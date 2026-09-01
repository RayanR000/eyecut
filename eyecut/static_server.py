"""Static file server that honours HTTP Range requests.

Python's `http.server` ignores `Range` entirely and always replies 200 with the
whole file. A `<video>` element treats that as "not seekable", so scrubbing and
seeking silently do nothing -- which is most of what a footage picker needs to do.
This adds 206 Partial Content, and nothing else.

    python -m eyecut.static_server --root ./browser --port 8731

The root is served as-is; a `.root` sentinel file is written so a client can tell
this server apart from an unrelated one squatting the port. That check matters:
reusing a stale server rooted one directory deeper 404s every page while looking
perfectly healthy.
"""
from __future__ import annotations

import argparse
import http.server
import os
import re
import socketserver
import threading
from pathlib import Path

SENTINEL = ".root"
SENTINEL_BODY = "eyecut-static-root"
RANGE_RE = re.compile(r"bytes=(\d+)-(\d*)")


class _Clip:
    """File wrapper that stops after n bytes, for `copyfile()`."""

    def __init__(self, f, n: int):
        self.f, self.n = f, n

    def read(self, size: int = -1) -> bytes:
        if self.n <= 0:
            return b""
        if size < 0 or size > self.n:
            size = self.n
        data = self.f.read(size)
        self.n -= len(data)
        return data

    def close(self):
        self.f.close()


class RangeHandler(http.server.SimpleHTTPRequestHandler):
    quiet = True

    def send_head(self):
        rng = self.headers.get("Range")
        if not rng:
            return super().send_head()
        path = self.translate_path(self.path)
        if os.path.isdir(path) or not os.path.exists(path):
            return super().send_head()
        m = RANGE_RE.match(rng)
        if not m:
            return super().send_head()

        size = os.path.getsize(path)
        start = int(m.group(1))
        end = int(m.group(2)) if m.group(2) else size - 1
        end = min(end, size - 1)
        if start > end:
            self.send_response(416)
            self.send_header("Content-Range", f"bytes */{size}")
            self.end_headers()
            return None

        f = open(path, "rb")
        f.seek(start)
        self.send_response(206)
        self.send_header("Content-Type", self.guess_type(path))
        self.send_header("Content-Range", f"bytes {start}-{end}/{size}")
        self.send_header("Content-Length", str(end - start + 1))
        self.send_header("Accept-Ranges", "bytes")
        self.end_headers()
        return _Clip(f, end - start + 1)

    def end_headers(self):
        # Advertise range support on every response, including the plain 200s --
        # a browser that never sees this header will not attempt to seek at all.
        if not any(b"Accept-Ranges" in h for h in (self._headers_buffer or [])):
            self.send_header("Accept-Ranges", "bytes")
        super().end_headers()

    def log_message(self, *args):
        if not self.quiet:
            super().log_message(*args)


class Server(socketserver.ThreadingTCPServer):
    allow_reuse_address = True
    daemon_threads = True


def serve(root: Path | str, port: int = 8731, *, background: bool = False,
          quiet: bool = True) -> Server:
    """Serve `root` on `port`. Returns the server (call `shutdown()` to stop)."""
    root = Path(root).resolve()
    root.mkdir(parents=True, exist_ok=True)
    (root / SENTINEL).write_text(SENTINEL_BODY)

    handler = type("Handler", (RangeHandler,),
                   {"quiet": quiet,
                    "__init__": lambda self, *a, **kw:
                        RangeHandler.__init__(self, *a, directory=str(root), **kw)})
    srv = Server(("127.0.0.1", port), handler)
    if background:
        threading.Thread(target=srv.serve_forever, daemon=True).start()
    return srv


def is_ours(port: int = 8731, timeout: float = 2.0) -> bool:
    """Is the server on `port` one of ours, rooted where we expect?

    A stale server rooted elsewhere answers requests happily and 404s every real
    page, which is indistinguishable from a broken build unless you check.
    """
    import urllib.error
    import urllib.request
    try:
        with urllib.request.urlopen(f"http://localhost:{port}/{SENTINEL}",
                                    timeout=timeout) as r:
            return r.read().decode().strip() == SENTINEL_BODY
    except (urllib.error.URLError, OSError, ValueError):
        return False


def main(argv=None) -> int:
    ap = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    ap.add_argument("--root", type=Path, default=Path.cwd())
    ap.add_argument("--port", type=int, default=8731)
    ap.add_argument("-v", "--verbose", action="store_true")
    a = ap.parse_args(argv)
    srv = serve(a.root, a.port, quiet=not a.verbose)
    print(f"serving {Path(a.root).resolve()} on http://localhost:{a.port}")
    try:
        srv.serve_forever()
    except KeyboardInterrupt:
        pass
    return 0


if __name__ == "__main__":
    raise SystemExit(main())

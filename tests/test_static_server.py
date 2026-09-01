"""Range support is the whole reason this server exists.

`http.server` answers every request with the full file and no `Accept-Ranges`, so
a `<video>` decides the source is not seekable and scrubbing silently does
nothing. These tests pin the two behaviours that fixed that, plus the root
sentinel -- a stale server rooted one directory deeper answers happily and 404s
every real page, which looks exactly like a broken build.
"""
import urllib.error
import urllib.request

import pytest

from eyecut.static_server import SENTINEL, is_ours, serve

BODY = bytes(range(256)) * 40          # 10240 bytes, every value distinct per block
PORT = 8797


@pytest.fixture
def server(tmp_path):
    (tmp_path / "big.bin").write_bytes(BODY)
    (tmp_path / "page.html").write_text("<h1>hi</h1>")
    srv = serve(tmp_path, PORT, background=True)
    yield tmp_path
    srv.shutdown()
    srv.server_close()


def get(path, headers=None):
    req = urllib.request.Request(f"http://localhost:{PORT}/{path}", headers=headers or {})
    return urllib.request.urlopen(req, timeout=5)


def test_range_request_returns_206_and_only_that_slice(server):
    with get("big.bin", {"Range": "bytes=10-19"}) as r:
        assert r.status == 206
        assert r.headers["Content-Range"] == f"bytes 10-19/{len(BODY)}"
        assert r.headers["Content-Length"] == "10"
        assert r.read() == BODY[10:20]


def test_open_ended_range_runs_to_the_end(server):
    start = len(BODY) - 5
    with get("big.bin", {"Range": f"bytes={start}-"}) as r:
        assert r.status == 206
        assert r.read() == BODY[start:]


def test_range_past_the_end_is_clamped(server):
    with get("big.bin", {"Range": "bytes=10230-99999"}) as r:
        assert r.status == 206
        assert r.read() == BODY[10230:]


def test_plain_get_still_advertises_range_support(server):
    """Without this header a browser will not attempt to seek at all."""
    with get("big.bin") as r:
        assert r.status == 200
        assert r.headers["Accept-Ranges"] == "bytes"
        assert r.read() == BODY


def test_unsatisfiable_range_is_416(server):
    with pytest.raises(urllib.error.HTTPError) as e:
        get("big.bin", {"Range": "bytes=99999-"})
    assert e.value.code == 416


def test_malformed_range_falls_back_to_whole_file(server):
    with get("big.bin", {"Range": "bytes=abc"}) as r:
        assert r.status == 200
        assert r.read() == BODY


def test_directory_range_request_does_not_crash(server):
    with get("", {"Range": "bytes=0-9"}) as r:
        assert r.status == 200


def test_sentinel_identifies_our_root(server):
    assert (server / SENTINEL).read_text().strip()
    assert is_ours(PORT) is True


def test_is_ours_false_when_nothing_is_listening():
    assert is_ours(PORT + 53) is False


def test_is_ours_false_for_a_server_rooted_elsewhere(tmp_path):
    """The real bug: a leftover server one level deeper 404s every page."""
    deeper = tmp_path / "sub"
    deeper.mkdir()
    srv = serve(deeper, PORT + 1, background=True)
    try:
        assert is_ours(PORT + 1) is True         # rooted at `deeper`, sentinel present
        (deeper / SENTINEL).unlink()             # now it is somebody else's server
        assert is_ours(PORT + 1) is False
    finally:
        srv.shutdown()
        srv.server_close()

"""Static description of the benchmark matrix (stdlib only).

Shared by the orchestrator (which must not import the HTTP libraries, since it
may run under a different interpreter) and the worker.
"""

from __future__ import annotations

PROTOCOLS = ("h1", "h2")
TRANSPORTS = ("tls", "plain")

# name -> metadata.  ``support`` maps transport -> protocols the client can speak.
#   tls   = HTTPS; h1/h2 negotiated through ALPN.
#   plain = cleartext; h1 = plain HTTP/1.1, h2 = h2c with prior knowledge.
CLIENTS: dict[str, dict] = {
    "pycurl": {
        "dist": "pycurl",
        "kind": "sync",
        "support": {"tls": ("h1", "h2"), "plain": ("h1", "h2")},
        "notes": "libcurl easy handle for sequential work; CurlMulti (no threads) for concurrency.",
    },
    "httpx2": {
        "dist": "httpx2",
        "kind": "sync",
        "support": {"tls": ("h1", "h2"), "plain": ("h1", "h2")},
        "notes": "httpx2.Client; concurrency via threads sharing one client. HTTP/2 from many threads "
        "is intermittently unsafe: httpcore2 can emit stream IDs out of order and the server "
        "answers GOAWAY(PROTOCOL_ERROR) (seen in ~1 of 5 `concurrent_small` h2 runs).",
    },
    "httpx2-async": {
        "dist": "httpx2",
        "kind": "async",
        "support": {"tls": ("h1", "h2"), "plain": ("h1", "h2")},
        "notes": "httpx2.AsyncClient on asyncio.",
    },
    "httpcore2": {
        "dist": "httpcore2",
        "kind": "sync",
        "support": {"tls": ("h1", "h2"), "plain": ("h1", "h2")},
        "notes": "httpcore2.ConnectionPool directly (the transport underneath httpx2); same "
        "threaded-HTTP/2 stream-ordering race as httpx2.",
    },
    "httpcore2-async": {
        "dist": "httpcore2",
        "kind": "async",
        "support": {"tls": ("h1", "h2"), "plain": ("h1", "h2")},
        "notes": "httpcore2.AsyncConnectionPool on asyncio.",
    },
    "urllib3": {
        "dist": "urllib3",
        "kind": "sync",
        "support": {"tls": ("h1", "h2"), "plain": ("h1",)},
        "notes": "PoolManager; h2 = experimental urllib3.http2.inject_into_urllib3() "
        "(TLS only, one request per connection at a time, no multiplexing, request bodies "
        "larger than the peer's flow-control window are rejected).",
    },
    "requests": {
        "dist": "requests",
        "kind": "sync",
        "support": {"tls": ("h1",), "plain": ("h1",)},
        "notes": "Session + HTTPAdapter. HTTP/1.1 only: requests has no HTTP/2 support, and "
        "urllib3's experimental h2 connection rejects the arguments requests passes.",
    },
    "aiohttp": {
        "dist": "aiohttp",
        "kind": "async",
        "support": {"tls": ("h1",), "plain": ("h1",)},
        "notes": "ClientSession; aiohttp has no HTTP/2 client support.",
    },
}

# Scenario defaults (``count`` = requests per measured round).
SCENARIOS: dict[str, dict] = {
    "seq_small": {
        "desc": "Sequential GET of a 1 KiB body over one keep-alive connection",
        "path": "/1k",
        "method": "GET",
        "count": 2000,
        "unit": "req/s",
    },
    "concurrent_small": {
        "desc": "GET 1 KiB with N in-flight requests (threads / tasks / curl multi)",
        "path": "/1k",
        "method": "GET",
        "count": 4000,
        "unit": "req/s",
    },
    "download_large": {
        "desc": "Sequential GET of a 10 MiB body",
        "path": "/10m",
        "method": "GET",
        "count": 50,
        "unit": "MiB/s",
    },
    "upload": {
        "desc": "Sequential POST of a 1 MiB body",
        "path": "/upload",
        "method": "POST",
        "count": 300,
        "unit": "MiB/s",
    },
    "cold": {
        "desc": "New client per request: connect + TLS handshake + 1 KiB GET + close",
        "path": "/1k",
        "method": "GET",
        "count": 500,
        "unit": "req/s",
    },
}

SMALL_SIZE = 1024
LARGE_SIZE = 10 * 1024 * 1024
UPLOAD_SIZE = 1024 * 1024

# Expected response-body length per path (``/upload`` replies ``ok``).
EXPECTED_LEN = {"/1k": SMALL_SIZE, "/10m": LARGE_SIZE, "/upload": 2}


def supported(client: str, proto: str, transport: str) -> bool:
    return proto in CLIENTS[client]["support"].get(transport, ())

"""Thin, uniform adapters around each HTTP client library.

Every adapter is configured the way a performance-conscious user would
configure it (persistent client / connection pool, keep-alive, explicit
HTTP version), and with the *same* TLS trust settings: a pre-built
``ssl.SSLContext`` that trusts the benchmark CA for the libraries built on the
``ssl`` module, and ``CAINFO`` pointing at the same CA for libcurl.

Each adapter returns the response body as ``bytes`` and raises on a non-200
status, so the worker can validate every single response.
"""

from __future__ import annotations

import io
import ssl
import threading
from dataclasses import dataclass

TIMEOUT = 60.0


@dataclass(frozen=True)
class Target:
    base_url: str  # e.g. https://127.0.0.1:8443
    proto: str  # "h1" | "h2"
    transport: str  # "tls" | "plain"
    ca_file: str

    def ssl_context(self) -> ssl.SSLContext:
        return ssl.create_default_context(cafile=self.ca_file)


class StatusError(RuntimeError):
    pass


def _check(status: int, url: str) -> None:
    if status != 200:
        raise StatusError(f"HTTP {status} for {url}")


# ---------------------------------------------------------------------------
# pycurl
# ---------------------------------------------------------------------------


class PycurlClient:
    kind = "sync"

    def __init__(self, t: Target, max_conns: int, ssl_ctx: ssl.SSLContext | None = None):
        import pycurl

        self._pycurl = pycurl
        self.t = t
        self.max_conns = max_conns
        if t.proto == "h1":
            self._ver = pycurl.CURL_HTTP_VERSION_1_1
        elif t.transport == "tls":
            self._ver = pycurl.CURL_HTTP_VERSION_2TLS
        else:
            self._ver = pycurl.CURL_HTTP_VERSION_2_PRIOR_KNOWLEDGE
        self._easy = self._new_handle()
        self._multi = None
        self._multi_handles: list = []

    def _new_handle(self):
        pycurl = self._pycurl
        c = pycurl.Curl()
        c.setopt(pycurl.HTTP_VERSION, self._ver)
        c.setopt(pycurl.NOSIGNAL, 1)
        c.setopt(pycurl.TCP_NODELAY, 1)
        c.setopt(pycurl.NOPROXY, "*")
        c.setopt(pycurl.TIMEOUT, int(TIMEOUT))
        # Do not send "Expect: 100-continue" for large bodies; no other client does.
        c.setopt(pycurl.HTTPHEADER, ["Expect:"])
        if self.t.transport == "tls":
            c.setopt(pycurl.CAINFO, self.t.ca_file)
            c.setopt(pycurl.SSL_VERIFYPEER, 1)
            c.setopt(pycurl.SSL_VERIFYHOST, 2)
        return c

    def get(self, url: str) -> bytes:
        pycurl = self._pycurl
        c = self._easy
        buf = io.BytesIO()
        c.setopt(pycurl.HTTPGET, 1)
        c.setopt(pycurl.URL, url)
        c.setopt(pycurl.WRITEDATA, buf)
        c.perform()
        _check(c.getinfo(pycurl.RESPONSE_CODE), url)
        return buf.getvalue()

    def post(self, url: str, body: bytes) -> bytes:
        pycurl = self._pycurl
        c = self._easy
        buf = io.BytesIO()
        c.setopt(pycurl.URL, url)
        c.setopt(pycurl.POSTFIELDSIZE_LARGE, len(body))
        c.setopt(pycurl.POSTFIELDS, body)
        c.setopt(pycurl.WRITEDATA, buf)
        c.perform()
        _check(c.getinfo(pycurl.RESPONSE_CODE), url)
        return buf.getvalue()

    def concurrent_get(self, url: str, n: int, c: int, expected_len: int) -> None:
        """Native libcurl concurrency: one CurlMulti driving ``c`` easy handles."""
        pycurl = self._pycurl
        if self._multi is None:
            m = pycurl.CurlMulti()
            if self.t.proto == "h2":
                m.setopt(pycurl.M_PIPELINING, pycurl.PIPE_MULTIPLEX)
            else:
                m.setopt(pycurl.M_PIPELINING, 0)
                m.setopt(pycurl.M_MAX_HOST_CONNECTIONS, c)
            m.setopt(pycurl.M_MAXCONNECTS, c)
            for _ in range(c):
                h = self._new_handle()
                h.chunks = []
                h.setopt(pycurl.WRITEFUNCTION, h.chunks.append)
                h.setopt(pycurl.URL, url)
                if self.t.proto == "h2":
                    # Wait for the existing connection to be usable for
                    # multiplexing rather than opening a new one per handle.
                    h.setopt(pycurl.PIPEWAIT, 1)
                self._multi_handles.append(h)
            self._multi = m
        m = self._multi
        free = list(self._multi_handles)
        remaining = n
        active = 0
        while remaining or active:
            while free and remaining:
                h = free.pop()
                h.chunks.clear()
                m.add_handle(h)
                remaining -= 1
                active += 1
            while True:
                ret, _ = m.perform()
                if ret != pycurl.E_CALL_MULTI_PERFORM:
                    break
            while True:
                queued, ok, err = m.info_read()
                for h in ok:
                    _check(h.getinfo(pycurl.RESPONSE_CODE), url)
                    got = sum(map(len, h.chunks))
                    if got != expected_len:
                        raise AssertionError(f"body length {got} != {expected_len}")
                    m.remove_handle(h)
                    free.append(h)
                    active -= 1
                if err:
                    h, errno, msg = err[0]
                    raise RuntimeError(f"curl error {errno}: {msg}")
                if not queued:
                    break
            if active:
                m.select(1.0)

    def close(self) -> None:
        if self._multi is not None:
            for h in self._multi_handles:
                h.close()
            self._multi.close()
        self._easy.close()


# ---------------------------------------------------------------------------
# httpx2 / httpcore2
# ---------------------------------------------------------------------------


def _httpx_flags(t: Target) -> dict:
    if t.proto == "h1":
        return {"http1": True, "http2": False}
    if t.transport == "tls":
        return {"http1": True, "http2": True}  # ALPN picks h2
    return {"http1": False, "http2": True}  # h2c, prior knowledge


class Httpx2Client:
    kind = "sync"

    def __init__(self, t: Target, max_conns: int, ssl_ctx: ssl.SSLContext | None = None):
        import httpx2

        self.client = httpx2.Client(
            verify=ssl_ctx or t.ssl_context(),
            trust_env=False,
            timeout=TIMEOUT,
            limits=httpx2.Limits(max_connections=max_conns, max_keepalive_connections=max_conns),
            **_httpx_flags(t),
        )

    def get(self, url: str) -> bytes:
        r = self.client.get(url)
        _check(r.status_code, url)
        return r.content

    def post(self, url: str, body: bytes) -> bytes:
        r = self.client.post(url, content=body)
        _check(r.status_code, url)
        return r.content

    def close(self) -> None:
        self.client.close()


class Httpx2AsyncClient:
    kind = "async"

    def __init__(self, t: Target, max_conns: int, ssl_ctx: ssl.SSLContext | None = None):
        import httpx2

        self.client = httpx2.AsyncClient(
            verify=ssl_ctx or t.ssl_context(),
            trust_env=False,
            timeout=TIMEOUT,
            limits=httpx2.Limits(max_connections=max_conns, max_keepalive_connections=max_conns),
            **_httpx_flags(t),
        )

    async def get(self, url: str) -> bytes:
        r = await self.client.get(url)
        _check(r.status_code, url)
        return r.content

    async def post(self, url: str, body: bytes) -> bytes:
        r = await self.client.post(url, content=body)
        _check(r.status_code, url)
        return r.content

    async def close(self) -> None:
        await self.client.aclose()


class Httpcore2Client:
    kind = "sync"

    def __init__(self, t: Target, max_conns: int, ssl_ctx: ssl.SSLContext | None = None):
        import httpcore2

        self.pool = httpcore2.ConnectionPool(
            ssl_context=ssl_ctx or t.ssl_context(),
            max_connections=max_conns,
            max_keepalive_connections=max_conns,
            **_httpx_flags(t),
        )
        self.ext = {"timeout": {"connect": TIMEOUT, "read": TIMEOUT, "write": TIMEOUT, "pool": TIMEOUT}}

    def get(self, url: str) -> bytes:
        r = self.pool.request("GET", url, extensions=self.ext)
        _check(r.status, url)
        return r.content

    def post(self, url: str, body: bytes) -> bytes:
        r = self.pool.request("POST", url, content=body, extensions=self.ext)
        _check(r.status, url)
        return r.content

    def close(self) -> None:
        self.pool.close()


class Httpcore2AsyncClient:
    kind = "async"

    def __init__(self, t: Target, max_conns: int, ssl_ctx: ssl.SSLContext | None = None):
        import httpcore2

        self.pool = httpcore2.AsyncConnectionPool(
            ssl_context=ssl_ctx or t.ssl_context(),
            max_connections=max_conns,
            max_keepalive_connections=max_conns,
            **_httpx_flags(t),
        )
        self.ext = {"timeout": {"connect": TIMEOUT, "read": TIMEOUT, "write": TIMEOUT, "pool": TIMEOUT}}

    async def get(self, url: str) -> bytes:
        r = await self.pool.request("GET", url, extensions=self.ext)
        _check(r.status, url)
        return r.content

    async def post(self, url: str, body: bytes) -> bytes:
        r = await self.pool.request("POST", url, content=body, extensions=self.ext)
        _check(r.status, url)
        return r.content

    async def close(self) -> None:
        await self.pool.aclose()


# ---------------------------------------------------------------------------
# urllib3 / requests
# ---------------------------------------------------------------------------

_h2_injected = False
_h2_lock = threading.Lock()


def _maybe_inject_urllib3_h2(t: Target) -> None:
    """urllib3's HTTP/2 support is an experimental, process-global switch."""
    global _h2_injected
    if t.proto != "h2":
        return
    with _h2_lock:
        if not _h2_injected:
            import urllib3.http2

            urllib3.http2.inject_into_urllib3()
            _h2_injected = True


class Urllib3Client:
    kind = "sync"

    def __init__(self, t: Target, max_conns: int, ssl_ctx: ssl.SSLContext | None = None):
        import urllib3

        _maybe_inject_urllib3_h2(t)
        self.pm = urllib3.PoolManager(
            maxsize=max_conns,
            block=True,
            ssl_context=ssl_ctx or t.ssl_context(),
            retries=False,
            timeout=urllib3.Timeout(TIMEOUT),
        )

    def get(self, url: str) -> bytes:
        r = self.pm.request("GET", url)
        _check(r.status, url)
        return r.data

    def post(self, url: str, body: bytes) -> bytes:
        r = self.pm.request("POST", url, body=body)
        _check(r.status, url)
        return r.data

    def close(self) -> None:
        self.pm.clear()


class RequestsClient:
    kind = "sync"

    def __init__(self, t: Target, max_conns: int, ssl_ctx: ssl.SSLContext | None = None):
        import requests
        from requests.adapters import HTTPAdapter

        # Idiomatic requests configuration.  Note that requests has no API for
        # a pre-built SSLContext: with ``verify=<path>`` it hands the CA path to
        # urllib3, which loads it again for every *new* connection.  That cost
        # is part of requests' real-world behaviour and shows up in "cold".
        s = requests.Session()
        s.trust_env = False
        s.verify = t.ca_file
        adapter = HTTPAdapter(pool_connections=1, pool_maxsize=max_conns, pool_block=True)
        s.mount("https://", adapter)
        s.mount("http://", adapter)
        self.s = s

    def get(self, url: str) -> bytes:
        r = self.s.get(url, timeout=TIMEOUT)
        _check(r.status_code, url)
        return r.content

    def post(self, url: str, body: bytes) -> bytes:
        r = self.s.post(url, data=body, timeout=TIMEOUT)
        _check(r.status_code, url)
        return r.content

    def close(self) -> None:
        self.s.close()


# ---------------------------------------------------------------------------
# aiohttp
# ---------------------------------------------------------------------------


class AiohttpClient:
    kind = "async"

    def __init__(self, t: Target, max_conns: int, ssl_ctx: ssl.SSLContext | None = None):
        import aiohttp

        if t.proto != "h1":
            raise NotImplementedError("aiohttp has no HTTP/2 client")
        # ClientSession must be created inside the running loop.
        connector = aiohttp.TCPConnector(
            limit=max_conns,
            ssl=(ssl_ctx or t.ssl_context()) if t.transport == "tls" else False,
        )
        self.s = aiohttp.ClientSession(
            connector=connector,
            trust_env=False,
            timeout=aiohttp.ClientTimeout(total=TIMEOUT),
        )

    async def get(self, url: str) -> bytes:
        async with self.s.get(url) as r:
            body = await r.read()
            _check(r.status, url)
            return body

    async def post(self, url: str, body: bytes) -> bytes:
        async with self.s.post(url, data=body) as r:
            out = await r.read()
            _check(r.status, url)
            return out

    async def close(self) -> None:
        await self.s.close()


ADAPTERS = {
    "pycurl": PycurlClient,
    "httpx2": Httpx2Client,
    "httpx2-async": Httpx2AsyncClient,
    "httpcore2": Httpcore2Client,
    "httpcore2-async": Httpcore2AsyncClient,
    "urllib3": Urllib3Client,
    "requests": RequestsClient,
    "aiohttp": AiohttpClient,
}

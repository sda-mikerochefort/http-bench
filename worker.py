"""Run exactly one benchmark case in a fresh interpreter and print JSON.

Invoked by ``bench.py`` with the venv's interpreter, e.g.::

    .bench-work/venvs/3.14/bin/python worker.py run '{"client": "httpx2", ...}'
    .bench-work/venvs/3.14/bin/python worker.py env-info

One process per case keeps library global state (urllib3's HTTP/2 injection,
interned connection pools, allocator high-water marks, ...) from leaking
between cases.
"""

from __future__ import annotations

import asyncio
import gc
import json
import os
import platform
import resource
import ssl
import sys
import threading
import time
import traceback

import catalog
from clients import ADAPTERS, Target

PROXY_VARS = ("http_proxy", "https_proxy", "all_proxy", "no_proxy")


def _scrub_proxy_env() -> None:
    for k in list(os.environ):
        if k.lower() in PROXY_VARS:
            del os.environ[k]


def _percentiles(samples_ns: list[int]) -> dict[str, float]:
    if not samples_ns:
        return {}
    s = sorted(samples_ns)

    def pct(p: float) -> float:
        i = min(len(s) - 1, max(0, round(p / 100 * (len(s) - 1))))
        return s[i] / 1e3  # -> microseconds

    return {"p50_us": pct(50), "p90_us": pct(90), "p99_us": pct(99), "max_us": s[-1] / 1e3}


def _split(n: int, c: int) -> list[int]:
    base, extra = divmod(n, c)
    return [base + (1 if i < extra else 0) for i in range(c)]


def _validate(body: bytes, expected: int) -> None:
    if len(body) != expected:
        raise AssertionError(f"body length {len(body)} != {expected}")


# ---------------------------------------------------------------------------
# Sync scenario drivers
# ---------------------------------------------------------------------------


class SyncRunner:
    def __init__(self, cls, target: Target, concurrency: int):
        self.cls = cls
        self.t = target
        self.c = concurrency
        self.ctx = target.ssl_context() if target.transport == "tls" else None
        self.client = None

    def open(self, scenario: str) -> None:
        conns = self.c if scenario == "concurrent_small" else 1
        self.client = self.cls(self.t, conns, self.ctx)

    def close(self) -> None:
        self.client.close()

    def proto_check(self) -> str:
        return self.client.get(self.t.base_url + "/proto").decode()

    def run(self, scenario: str, n: int, lat: list[int] | None) -> None:
        url = self.t.base_url + catalog.SCENARIOS[scenario]["path"]
        expected = catalog.EXPECTED_LEN[catalog.SCENARIOS[scenario]["path"]]
        pc = time.perf_counter_ns
        if scenario in ("seq_small", "download_large"):
            get = self.client.get
            for _ in range(n):
                t0 = pc()
                body = get(url)
                t1 = pc()
                _validate(body, expected)
                if lat is not None:
                    lat.append(t1 - t0)
        elif scenario == "upload":
            post = self.client.post
            payload = UPLOAD_BODY
            for _ in range(n):
                t0 = pc()
                body = post(url, payload)
                t1 = pc()
                _validate(body, expected)
                if lat is not None:
                    lat.append(t1 - t0)
        elif scenario == "cold":
            for _ in range(n):
                t0 = pc()
                cl = self.cls(self.t, 1, self.ctx)
                try:
                    body = cl.get(url)
                finally:
                    cl.close()
                t1 = pc()
                _validate(body, expected)
                if lat is not None:
                    lat.append(t1 - t0)
        elif scenario == "concurrent_small":
            native = getattr(self.client, "concurrent_get", None)
            if native is not None:
                native(url, n, self.c, expected)
                return
            errors: list[BaseException] = []
            get = self.client.get

            def work(k: int) -> None:
                try:
                    for _ in range(k):
                        _validate(get(url), expected)
                except BaseException as e:  # noqa: BLE001
                    errors.append(e)

            threads = [threading.Thread(target=work, args=(k,)) for k in _split(n, self.c)]
            for th in threads:
                th.start()
            for th in threads:
                th.join()
            if errors:
                raise errors[0]
        else:
            raise ValueError(scenario)


# ---------------------------------------------------------------------------
# Async scenario drivers
# ---------------------------------------------------------------------------


class AsyncRunner:
    def __init__(self, cls, target: Target, concurrency: int):
        self.cls = cls
        self.t = target
        self.c = concurrency
        self.ctx = target.ssl_context() if target.transport == "tls" else None
        self.client = None

    async def open(self, scenario: str) -> None:
        conns = self.c if scenario == "concurrent_small" else 1
        self.client = self.cls(self.t, conns, self.ctx)

    async def close(self) -> None:
        await self.client.close()

    async def proto_check(self) -> str:
        return (await self.client.get(self.t.base_url + "/proto")).decode()

    async def run(self, scenario: str, n: int, lat: list[int] | None) -> None:
        url = self.t.base_url + catalog.SCENARIOS[scenario]["path"]
        expected = catalog.EXPECTED_LEN[catalog.SCENARIOS[scenario]["path"]]
        pc = time.perf_counter_ns
        if scenario in ("seq_small", "download_large"):
            get = self.client.get
            for _ in range(n):
                t0 = pc()
                body = await get(url)
                t1 = pc()
                _validate(body, expected)
                if lat is not None:
                    lat.append(t1 - t0)
        elif scenario == "upload":
            post = self.client.post
            payload = UPLOAD_BODY
            for _ in range(n):
                t0 = pc()
                body = await post(url, payload)
                t1 = pc()
                _validate(body, expected)
                if lat is not None:
                    lat.append(t1 - t0)
        elif scenario == "cold":
            for _ in range(n):
                t0 = pc()
                cl = self.cls(self.t, 1, self.ctx)
                try:
                    body = await cl.get(url)
                finally:
                    await cl.close()
                t1 = pc()
                _validate(body, expected)
                if lat is not None:
                    lat.append(t1 - t0)
        elif scenario == "concurrent_small":
            get = self.client.get

            async def work(k: int) -> None:
                for _ in range(k):
                    _validate(await get(url), expected)

            await asyncio.gather(*(work(k) for k in _split(n, self.c)))
        else:
            raise ValueError(scenario)


UPLOAD_BODY = b""


def _measure_round(fn) -> dict:
    gc.collect()
    w0, c0 = time.perf_counter(), time.process_time()
    fn()
    w1, c1 = time.perf_counter(), time.process_time()
    return {"wall_s": w1 - w0, "cpu_s": c1 - c0}


def run_case(spec: dict) -> dict:
    global UPLOAD_BODY
    _scrub_proxy_env()
    if spec.get("client_cpus"):
        os.sched_setaffinity(0, spec["client_cpus"])

    client, scenario = spec["client"], spec["scenario"]
    proto, transport = spec["proto"], spec["transport"]
    n, rounds, warmup = spec["count"], spec["rounds"], spec["warmup"]
    conc = spec["concurrency"]
    target = Target(spec["base_url"], proto, transport, spec["ca_file"])
    cls = ADAPTERS[client]
    if scenario == "upload":
        UPLOAD_BODY = os.urandom(catalog.UPLOAD_SIZE)

    sc = catalog.SCENARIOS[scenario]
    path = sc["path"]
    bytes_per_req = catalog.UPLOAD_SIZE if scenario == "upload" else catalog.EXPECTED_LEN[path]
    want_proto = "HTTP/2.0" if proto == "h2" else "HTTP/1.1"
    collect_lat = scenario != "concurrent_small"

    results: list[dict] = []
    lat: list[int] = []

    if cls.kind == "sync":
        r = SyncRunner(cls, target, conc)
        r.open(scenario)
        try:
            seen = r.proto_check()
            if seen != want_proto:
                raise AssertionError(f"negotiated {seen!r}, expected {want_proto!r}")
            if warmup:
                r.run(scenario, warmup, None)
            for _ in range(rounds):
                res = _measure_round(lambda: r.run(scenario, n, lat if collect_lat else None))
                results.append(res)
        finally:
            r.close()
    else:

        async def main() -> None:
            ar = AsyncRunner(cls, target, conc)
            await ar.open(scenario)
            try:
                seen = await ar.proto_check()
                if seen != want_proto:
                    raise AssertionError(f"negotiated {seen!r}, expected {want_proto!r}")
                if warmup:
                    await ar.run(scenario, warmup, None)
                for _ in range(rounds):
                    gc.collect()
                    w0, c0 = time.perf_counter(), time.process_time()
                    await ar.run(scenario, n, lat if collect_lat else None)
                    w1, c1 = time.perf_counter(), time.process_time()
                    results.append({"wall_s": w1 - w0, "cpu_s": c1 - c0})
            finally:
                await ar.close()

        asyncio.run(main())

    for res in results:
        res["requests"] = n
        res["bytes"] = n * bytes_per_req
    return {
        "ok": True,
        "rounds": results,
        "latency": _percentiles(lat),
        "maxrss_kib": resource.getrusage(resource.RUSAGE_SELF).ru_maxrss,
        "negotiated": want_proto,
    }


def _dist_version(name: str) -> str | None:
    from importlib.metadata import PackageNotFoundError, version

    try:
        return version(name)
    except PackageNotFoundError:
        return None


def env_info() -> dict:
    info = {
        "python": platform.python_version(),
        "implementation": platform.python_implementation(),
        "build": " ".join(platform.python_build()),
        "compiler": platform.python_compiler(),
        "executable": sys.executable,
        "gil_enabled": getattr(sys, "_is_gil_enabled", lambda: True)(),
        "jit": _jit_status(),
        "openssl": ssl.OPENSSL_VERSION,
        "packages": {
            d: _dist_version(d)
            for d in (
                "pycurl", "httpx2", "httpcore2", "urllib3", "requests", "aiohttp",
                "h2", "h11", "hpack", "hyperframe", "anyio", "truststore", "certifi",
                "idna", "charset-normalizer", "multidict", "yarl", "propcache", "frozenlist",
            )
        },
    }
    try:
        import pycurl

        info["pycurl_version"] = pycurl.version
        info["libcurl_http2"] = bool(pycurl.version_info()[4] & pycurl.VERSION_HTTP2)
    except Exception as e:  # noqa: BLE001
        info["pycurl_version"] = f"unavailable: {e}"
    try:
        from aiohttp import http_parser

        info["aiohttp_c_parser"] = http_parser.HttpResponseParser.__module__.endswith("_http_parser")
    except Exception:  # noqa: BLE001
        info["aiohttp_c_parser"] = None
    return info


def _jit_status() -> str:
    jit = getattr(getattr(sys, "_jit", None), "is_enabled", None)
    if jit is None:
        return "n/a"
    return "enabled" if jit() else "available, disabled"


def main(argv: list[str]) -> int:
    if len(argv) >= 1 and argv[0] == "env-info":
        print(json.dumps(env_info()))
        return 0
    if len(argv) == 2 and argv[0] == "run":
        spec = json.loads(argv[1])
        try:
            out = run_case(spec)
        except BaseException as e:  # noqa: BLE001
            out = {"ok": False, "error": f"{type(e).__name__}: {e}", "traceback": traceback.format_exc()}
        print(json.dumps(out))
        return 0 if out["ok"] else 1
    print(__doc__, file=sys.stderr)
    return 2


if __name__ == "__main__":
    sys.exit(main(sys.argv[1:]))

# http-client-bench

A reproducible benchmark of Python HTTP client libraries over **HTTP/1.1 and
HTTP/2**, across **Python 3.12, 3.14 and 3.15**.

| Library | Variants benchmarked | HTTP/1.1 | HTTP/2 |
|---|---|---|---|
| [`pycurl`](https://pypi.org/project/pycurl/) | easy handle (sequential), `CurlMulti` (concurrent) | ✓ | ✓ (libcurl + nghttp2, multiplexed) |
| [`httpx2`](https://pypi.org/project/httpx2/) | `Client` (sync), `AsyncClient` | ✓ | ✓ (`http2=True`, multiplexed) |
| [`httpcore2`](https://pypi.org/project/httpcore2/) | `ConnectionPool` (sync), `AsyncConnectionPool` | ✓ | ✓ (`http2=True`, multiplexed) |
| [`urllib3`](https://pypi.org/project/urllib3/) | `PoolManager` | ✓ | ⚠ experimental `urllib3.http2.inject_into_urllib3()`: TLS only, one request per connection at a time, request bodies larger than the 64 KiB flow-control window fail |
| [`requests`](https://pypi.org/project/requests/) | `Session` | ✓ | ✗ no HTTP/2 support (urllib3's h2 connection rejects the arguments requests passes) |
| [`aiohttp`](https://pypi.org/project/aiohttp/) | `ClientSession` | ✓ | ✗ no HTTP/2 client |

`requirements.txt` is deliberately unpinned: every run installs the **latest**
release of each library into each interpreter's venv and records the exact
versions it got (and the libcurl / OpenSSL / h2 / h11 versions) in the results.

## Requirements

* Linux (macOS should work; CPU pinning is Linux-only and is skipped elsewhere)
* [uv](https://docs.astral.sh/uv/) **≥ 0.13**. Older uv releases don't know
  about the final 3.15 builds and may resolve `3.15` to a beta. The results
  table shows the exact interpreter version used.
* nginx ≥ 1.24 with the `http_v2` and `ssl` modules (`apt install nginx-light`,
  `brew install nginx`, …). The harness starts its own private instance on
  random loopback ports. It does not touch any system nginx.
* `openssl` CLI (used to mint a throwaway CA and server certificate)

The orchestrator only needs the standard library (Python ≥ 3.10) and can run
under any interpreter. The interpreters under test are uv-managed
(python-build-standalone) builds, so all three versions share the same build
toolchain and flags.

## Running

```bash
./bench.py --quick                       # ~5 min smoke test (10% workload, 2 rounds)
```

```bash
./bench.py                               # full run: 3.12 / 3.14 / 3.15, HTTPS, all clients
```

```bash
./bench.py --transports tls plain        # also cleartext HTTP/1.1 and h2c (prior knowledge)
```

```bash
./bench.py --pythons 3.14 3.14t 3.15 --clients httpx2 pycurl --scenarios seq_small concurrent_small
```

```bash
./bench.py report results/bench-YYYYMMDD-HHMMSS.json   # re-render Markdown/CSV
```

Useful options (`./bench.py -h` for all of them):

| option | default | meaning |
|---|---|---|
| `--pythons` | `3.12 3.14 3.15` | any uv Python request string, e.g. `3.14t` for free-threaded builds |
| `--clients`, `--protocols`, `--scenarios`, `--transports` | all, all, all, `tls` | select a subset of the matrix |
| `--rounds` | 5 | measured rounds per case (median reported) |
| `--scale` | 1.0 | multiply per-round request counts |
| `--concurrency` | 32 | in-flight requests in `concurrent_small` |
| `--server-cpus` / `--client-cpus` / `--no-pin` | auto | CPU pinning; with ≥ 4 CPUs, nginx gets the first half and the client the second half |
| `--shuffle` | off | randomise case order |
| `--no-install` / `--reinstall` | | reuse venvs as-is / rebuild them from scratch |

Outputs land in `results/`: `bench-<timestamp>.json` holds the raw per-round data
and is rewritten after every case, so partial runs are kept. The `.csv` has one
row per case and the `.md` is the human-readable report.

## Scenarios

| scenario | workload per round | metric |
|---|---|---|
| `seq_small` | 2,000 × `GET /1k` (1 KiB body) sequentially over one keep-alive connection | req/s, CPU µs/req, p50/p99 latency |
| `concurrent_small` | 4,000 × `GET /1k` with 32 requests in flight | req/s, CPU µs/req |
| `download_large` | 50 × `GET /10m` (10 MiB, incompressible) | MiB/s |
| `upload` | 300 × `POST /upload` with a 1 MiB body | MiB/s |
| `cold` | 500 × (create client → connect → TLS handshake → `GET /1k` → close) | req/s |

How concurrency is driven depends on the library's natural model:

* sync clients (httpx2, httpcore2, urllib3, requests): 32 threads sharing one client/pool
* async clients (httpx2-async, httpcore2-async, aiohttp): 32 coroutines on one asyncio loop
* pycurl: one `CurlMulti` driving 32 easy handles, with no threads. For HTTP/2 it
  uses `PIPE_MULTIPLEX` and `PIPEWAIT` so all streams share one connection.

With HTTP/1.1 the pools are sized to 32 connections. With HTTP/2 the
multiplexing clients (pycurl, httpx2, httpcore2) use a single connection, while
urllib3's experimental h2 still uses one connection per in-flight request.

## Methodology

* **Server.** A private nginx instance on 127.0.0.1 serves HTTPS (HTTP/1.1 and
  HTTP/2 negotiated through ALPN on the same port), cleartext HTTP/1.1, and
  cleartext h2c. A native server keeps the server out of the measurement, and
  it is identical for every interpreter. Notable settings:
  * `sendfile off`. TLS can't use sendfile anyway, and in nginx 1.24 it adds a
    ~40 ms delayed-ACK stall to every h2c response after the first. Turning it
    off everywhere keeps the server's send path the same for every
    protocol/transport combination.
  * `/upload` is a `proxy_pass` to an internal endpoint with
    `proxy_pass_request_body off`. That makes nginx read and buffer the whole
    1 MiB request body before answering. A bare `return 200` would reply early
    and never open the HTTP/2 flow-control window, which makes HTTP/2 uploads
    hang.
  * Unlimited keep-alive (`keepalive_requests`, `keepalive_timeout`), so
    connections are never recycled mid-run. `http2_max_concurrent_streams 256`.
* **CPU isolation.** On machines with ≥ 4 CPUs, nginx and the client process are
  pinned to disjoint halves of the CPUs.
* **Process isolation.** Every (python, transport, scenario, client, protocol)
  case runs in a fresh interpreter. This stops global state from leaking
  between cases, for example urllib3's process-wide HTTP/2 injection, warmed
  pools, or allocator high-water marks. Versions are interleaved, with all
  Pythons for one case run back to back, so slow machine drift affects them
  equally.
* **Warm-up and rounds.** Each case opens its client, performs a warm-up of 20 %
  of a round (which also establishes the connections), then runs `--rounds`
  measured rounds. The report shows the median throughput and the relative
  standard deviation across rounds (±). Rounds above ~10 % are worth re-running.
* **CPU cost.** `time.process_time()` (all threads) per round ÷ requests gives
  the client's CPU time per request. On loopback this is usually the number
  that explains the throughput differences.
* **Correctness checks.**
  * Every response's status (200) and body length are checked.
  * Before measuring, each case requests `/proto`, which echoes nginx's
    `$server_protocol`. The case fails unless the library really negotiated
    the intended protocol, so a client silently falling back from h2 to h1
    can't produce misleading numbers.
* **Equal TLS settings.** Certificate verification is on everywhere, against a
  throwaway P-256 CA. The ssl-module-based libraries (httpx2, httpcore2,
  urllib3, aiohttp) get one pre-built `ssl.SSLContext` per case. requests has
  no API for that, so it uses the idiomatic `session.verify = <ca file>`, which
  makes urllib3 reload the CA for each *new* connection. pycurl uses `CAINFO`.
  Proxy environment variables are removed and every library's env-proxy
  support is disabled.
* **Library configuration** is what a performance-conscious user would write:
  * persistent clients and keep-alive
  * pool sizes matched to the concurrency
  * no retries
  * explicit protocol selection: `http2=True` / `http1=False` (h2c) for
    httpx2/httpcore2, `CURL_HTTP_VERSION_2TLS` / `_2_PRIOR_KNOWLEDGE` for
    pycurl, and `Expect:` disabled so libcurl doesn't add a 100-continue round
    trip to uploads
  * no third-party event loops (uvloop) or optional speed-ups beyond what each
    library installs by default. aiohttp's C parser is used, and recorded in
    the report.

## Interpreting results and caveats

* **Loopback has ~0 RTT.** HTTP/2's main wins (multiplexing instead of
  connection setup and head-of-line queueing, HPACK header compression) grow
  with latency and with the number of connections avoided. On loopback, the
  per-request CPU cost of the pure-Python h2 state machine (`h2`/`hpack`)
  usually dominates. Expect HTTP/2 to look *worse* for Python clients here than
  over a real network, and pycurl's native nghttp2 to be the exception. To
  approximate a WAN, add delay on `lo` before running (root required):

  ```bash
  sudo tc qdisc add dev lo root netem delay 5ms
  ```

  and remove it afterwards:

  ```bash
  sudo tc qdisc del dev lo root
  ```

* **Known library issue surfaced by this benchmark.** Sharing one sync
  `httpx2.Client` or `httpcore2.ConnectionPool` over HTTP/2 between threads
  sometimes sends HEADERS frames with out-of-order stream IDs. nginx logs
  `client sent HEADERS frame with incorrect identifier …` and closes the
  connection with GOAWAY(PROTOCOL_ERROR). h2 also sometimes raises
  `LocalProtocolError: Invalid input StreamInputs.SEND_HEADERS`. It's
  intermittent (about 1 in 5 `concurrent_small` h2 runs) and appears on every
  Python version. The harness reports such cases as failed instead of retrying
  them. The async clients are not affected.
* Sync clients use threads for concurrency, so they are bounded by the GIL.
  Try the free-threaded builds with `--pythons 3.14t 3.15t`. Not every library
  ships free-threaded wheels; build failures are reported per case.
* Cloud VMs and laptops have noisy neighbours, CPU frequency scaling and
  thermal throttling. For publishable numbers, use a quiet bare-metal host, a
  fixed CPU frequency governor, `--rounds 10` and possibly `--shuffle`, and
  compare the ± column before drawing conclusions.
* `cold` measures client construction plus a full TCP+TLS handshake, so it
  includes library-specific one-off costs such as CA loading.

## Layout

```
bench.py         orchestrator: venvs (uv), nginx lifecycle, case matrix, result files
worker.py        runs one case inside the interpreter under test, prints JSON
clients.py       one small adapter class per library/variant
catalog.py       the matrix: clients, supported protocols, scenarios, sizes
server.py        nginx config generation, test CA/certs, payload files
report.py        JSON → Markdown/CSV (also: python report.py results/*.json)
requirements.txt the libraries under test (unpinned → latest)
```

To add a library:

1. Write an adapter class in `clients.py` with `get` / `post` / `close`, sync
   or async, and add it to `ADAPTERS`.
2. Add an entry to `catalog.CLIENTS` that says which protocols it supports.

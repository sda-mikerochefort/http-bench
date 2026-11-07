"""Local nginx origin server (HTTP/1.1, HTTP/2 over TLS, h2c) + test CA.

nginx is used rather than a Python server so that the server is never the
bottleneck and is identical for every interpreter under test.
"""

from __future__ import annotations

import os
import re
import shutil
import signal
import socket
import subprocess
import time
from dataclasses import dataclass
from pathlib import Path

import catalog


def _free_port() -> int:
    with socket.socket() as s:
        s.bind(("127.0.0.1", 0))
        return s.getsockname()[1]


def make_certs(d: Path) -> tuple[Path, Path, Path]:
    """Create a throwaway CA and a P-256 server cert for 127.0.0.1/localhost.

    Extensions are set so the chain passes Python 3.13+'s VERIFY_X509_STRICT.
    """
    d.mkdir(parents=True, exist_ok=True)
    ca_key, ca_crt = d / "ca.key", d / "ca.crt"
    key, csr, crt, ext = d / "server.key", d / "server.csr", d / "server.crt", d / "server.ext"
    if ca_crt.exists() and crt.exists() and key.exists():
        return ca_crt, crt, key
    run = lambda *a: subprocess.run(a, check=True, capture_output=True)  # noqa: E731
    ec = ("-newkey", "ec", "-pkeyopt", "ec_paramgen_curve:prime256v1", "-nodes")
    run("openssl", "req", "-x509", *ec, "-keyout", str(ca_key), "-out", str(ca_crt), "-days", "30",
        "-subj", "/CN=http-client-bench CA",
        "-addext", "basicConstraints=critical,CA:TRUE",
        "-addext", "keyUsage=critical,keyCertSign,cRLSign",
        "-addext", "subjectKeyIdentifier=hash")
    run("openssl", "req", *ec, "-keyout", str(key), "-out", str(csr), "-subj", "/CN=127.0.0.1")
    ext.write_text(
        "basicConstraints=critical,CA:FALSE\n"
        "keyUsage=critical,digitalSignature\n"
        "extendedKeyUsage=serverAuth\n"
        "subjectAltName=IP:127.0.0.1,DNS:localhost\n"
        "subjectKeyIdentifier=hash\n"
        "authorityKeyIdentifier=keyid\n"
    )
    run("openssl", "x509", "-req", "-in", str(csr), "-CA", str(ca_crt), "-CAkey", str(ca_key),
        "-CAcreateserial", "-out", str(crt), "-days", "30", "-extfile", str(ext))
    return ca_crt, crt, key


def make_payloads(d: Path) -> None:
    d.mkdir(parents=True, exist_ok=True)
    for name, size in (("1k", catalog.SMALL_SIZE), ("10m", catalog.LARGE_SIZE)):
        p = d / name
        if not p.exists() or p.stat().st_size != size:
            p.write_bytes(os.urandom(size))  # incompressible


def nginx_version(nginx: str) -> tuple[int, int, int]:
    out = subprocess.run([nginx, "-v"], capture_output=True, text=True).stderr
    m = re.search(r"nginx/(\d+)\.(\d+)\.(\d+)", out)
    if not m:
        raise RuntimeError(f"cannot parse nginx version from {out!r}")
    return tuple(int(x) for x in m.groups())  # type: ignore[return-value]


_LOCATIONS = """
        root {data};
        default_type application/octet-stream;
        location = /proto  {{ default_type text/plain; return 200 "$server_protocol"; }}
        # proxy_pass forces nginx to read (and buffer in memory) the whole
        # request body before answering; a bare `return` would reply early and
        # leave HTTP/2 uploads stuck on flow control.  The body is not forwarded.
        location = /upload {{
            proxy_pass http://sink/ok;
            proxy_http_version 1.1;
            proxy_set_header Connection "";
            proxy_set_header Content-Length "";
            proxy_pass_request_body off;
        }}
        location / {{ }}
"""


def render_conf(*, workdir: Path, data: Path, crt: Path, key: Path, ports: dict[str, int],
                workers: int, version: tuple[int, int, int]) -> str:
    new_syntax = version >= (1, 25, 1)  # "http2 on;" replaced "listen ... http2"
    h2_listen = "" if new_syntax else " http2"
    h2_on = "        http2 on;\n" if new_syntax else ""
    locs = _LOCATIONS.format(data=data)
    user = "user root;\n" if os.geteuid() == 0 else ""
    return f"""{user}worker_processes {workers};
daemon off;
pid {workdir}/nginx.pid;
error_log {workdir}/error.log warn;
events {{ worker_connections 16384; }}
http {{
    access_log off;
    # sendfile off: TLS cannot use it anyway, and with nginx 1.24 it adds a
    # ~40 ms stall to every h2c response after the first.  Off everywhere keeps
    # the server send path identical for all protocol/transport combinations.
    sendfile off;
    tcp_nodelay on;
    keepalive_timeout 600s;
    keepalive_requests 10000000;
    client_max_body_size 64m;
    client_body_buffer_size 2m;
    open_file_cache max=64 inactive=600s;
    http2_max_concurrent_streams 256;
    client_body_temp_path {workdir}/tmp/body;
    proxy_temp_path {workdir}/tmp/proxy;
    fastcgi_temp_path {workdir}/tmp/fastcgi;
    uwsgi_temp_path {workdir}/tmp/uwsgi;
    scgi_temp_path {workdir}/tmp/scgi;
    ssl_protocols TLSv1.2 TLSv1.3;
    ssl_session_cache shared:SSL:10m;

    upstream sink {{ server 127.0.0.1:{ports['sink']}; keepalive 64; }}
    server {{   # internal upstream for /upload
        listen 127.0.0.1:{ports['sink']};
        location = /ok {{ default_type text/plain; return 200 "ok"; }}
    }}
    server {{   # HTTPS: HTTP/1.1 and HTTP/2 via ALPN
        listen 127.0.0.1:{ports['tls']} ssl{h2_listen};
{h2_on}        ssl_certificate {crt};
        ssl_certificate_key {key};
{locs}    }}
    server {{   # cleartext HTTP/1.1
        listen 127.0.0.1:{ports['plain_h1']};
{locs}    }}
    server {{   # cleartext HTTP/2 (h2c, prior knowledge)
        listen 127.0.0.1:{ports['plain_h2']}{h2_listen};
{h2_on}{locs}    }}
}}
"""


@dataclass
class Server:
    proc: subprocess.Popen
    ports: dict[str, int]
    ca_file: Path
    version: str

    def base_url(self, proto: str, transport: str) -> str:
        if transport == "tls":
            return f"https://127.0.0.1:{self.ports['tls']}"
        return f"http://127.0.0.1:{self.ports['plain_' + proto]}"

    def stop(self) -> None:
        if self.proc.poll() is None:
            self.proc.send_signal(signal.SIGQUIT)
            try:
                self.proc.wait(10)
            except subprocess.TimeoutExpired:
                self.proc.kill()


def start(workdir: Path, nginx: str = "nginx", cpus: list[int] | None = None) -> Server:
    nginx_bin = shutil.which(nginx) or nginx
    if not Path(nginx_bin).exists():
        raise SystemExit(
            f"nginx not found ({nginx!r}). Install it (e.g. `apt install nginx-light`, "
            "`brew install nginx`) or pass --nginx /path/to/nginx."
        )
    workdir = workdir.resolve()
    sdir = workdir / "server"
    for sub in ("tmp/body", "tmp/proxy", "tmp/fastcgi", "tmp/uwsgi", "tmp/scgi"):
        (sdir / sub).mkdir(parents=True, exist_ok=True)
    ca, crt, key = make_certs(workdir / "certs")
    data = workdir / "data"
    make_payloads(data)
    ver = nginx_version(nginx_bin)
    ports = {name: _free_port() for name in ("tls", "plain_h1", "plain_h2", "sink")}
    conf = sdir / "nginx.conf"
    conf.write_text(render_conf(workdir=sdir, data=data, crt=crt, key=key, ports=ports,
                                workers=max(1, len(cpus) if cpus else 2), version=ver))
    subprocess.run([nginx_bin, "-t", "-q", "-p", str(sdir), "-e", str(sdir / "error.log"),
                    "-c", str(conf)], check=True)
    preexec = (lambda: os.sched_setaffinity(0, cpus)) if cpus else None
    proc = subprocess.Popen(
        [nginx_bin, "-p", str(sdir), "-e", str(sdir / "error.log"), "-c", str(conf)],
        preexec_fn=preexec, stdout=subprocess.DEVNULL, stderr=subprocess.PIPE,
    )
    deadline = time.monotonic() + 10
    pending = set(ports.values())
    while pending and time.monotonic() < deadline:
        if proc.poll() is not None:
            raise RuntimeError(f"nginx exited: {proc.stderr.read().decode()}")
        for p in list(pending):
            try:
                socket.create_connection(("127.0.0.1", p), timeout=0.2).close()
                pending.discard(p)
            except OSError:
                pass
        time.sleep(0.05)
    if pending:
        proc.kill()
        raise RuntimeError("nginx did not start listening in time")
    return Server(proc, ports, ca, ".".join(map(str, ver)))

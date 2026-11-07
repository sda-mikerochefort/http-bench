#!/usr/bin/env python3
"""HTTP/1.1 vs HTTP/2 Python client benchmark - orchestrator.

Creates one uv-managed virtualenv per Python version with the latest release
of every library, starts a local nginx origin (HTTPS with ALPN h2/http1.1,
cleartext HTTP/1.1, cleartext h2c), then runs every
(python x transport x scenario x client x protocol) case in its own worker
process and writes JSON / CSV / Markdown results.

Only the standard library is needed to run this file (Python >= 3.10).

    ./bench.py                       # full run: 3.12, 3.14, 3.15; TLS
    ./bench.py --quick               # smoke run, ~10x smaller
    ./bench.py --pythons 3.14 3.15 --clients httpx2 pycurl --scenarios seq_small
    ./bench.py --transports tls plain
    ./bench.py report results/<file>.json
"""

from __future__ import annotations

import argparse
import datetime as dt
import json
import os
import platform
import random
import shutil
import subprocess
import sys
import time
from pathlib import Path

import catalog
import report
import server

HERE = Path(__file__).resolve().parent
WORKER = HERE / "worker.py"
REQUIREMENTS = HERE / "requirements.txt"
PROXY_VARS = {"http_proxy", "https_proxy", "all_proxy", "no_proxy"}


# ---------------------------------------------------------------------------
# helpers
# ---------------------------------------------------------------------------


def log(msg: str) -> None:
    print(msg, file=sys.stderr, flush=True)


def parse_cpus(text: str) -> list[int]:
    cpus: list[int] = []
    for part in text.split(","):
        if "-" in part:
            lo, hi = part.split("-")
            cpus.extend(range(int(lo), int(hi) + 1))
        elif part:
            cpus.append(int(part))
    return cpus


def worker_env() -> dict[str, str]:
    env = {k: v for k, v in os.environ.items() if k.lower() not in PROXY_VARS}
    env.pop("PYTHONPATH", None)
    env["PYTHONDONTWRITEBYTECODE"] = "1"
    return env


def uv_bin(arg: str | None) -> str:
    uv = arg or shutil.which("uv")
    if not uv:
        raise SystemExit("uv is required (https://docs.astral.sh/uv/); pass --uv /path/to/uv")
    return uv


def venv_python(venv: Path) -> Path:
    return venv / ("Scripts/python.exe" if os.name == "nt" else "bin/python")


def setup_env(spec: str, workdir: Path, uv: str, managed: bool, reinstall: bool) -> Path:
    venv = workdir / "venvs" / spec
    py = venv_python(venv)
    if reinstall and venv.exists():
        shutil.rmtree(venv)
    if not py.exists():
        cmd = [uv, "venv", "--quiet", "--python", spec, str(venv)]
        if managed:
            cmd.insert(2, "--managed-python")
        log(f"  creating venv for Python {spec}")
        subprocess.run(cmd, check=True)
    log(f"  installing latest libraries into Python {spec}")
    base = [uv, "pip", "install", "--quiet", "--upgrade", "--python", str(py)]
    if subprocess.run([*base, "-r", str(REQUIREMENTS)]).returncode != 0:
        # Typically one library has no wheel for this interpreter (e.g. a
        # free-threaded build) and fails to compile.  Install the rest one by
        # one; cases for the missing library will then fail individually.
        log(f"  bulk install failed for Python {spec}; installing requirements individually")
        for line in REQUIREMENTS.read_text().splitlines():
            req = line.split("#", 1)[0].strip()
            if req and subprocess.run([*base, req]).returncode != 0:
                log(f"  WARNING: could not install {req!r} for Python {spec}")
    return py


def env_info(py: Path) -> dict:
    out = subprocess.run([str(py), str(WORKER), "env-info"], check=True, capture_output=True,
                         text=True, cwd=HERE, env=worker_env())
    return json.loads(out.stdout)


def machine_info(srv: server.Server, uv: str, server_cpus, client_cpus) -> dict:
    cpu_model = None
    try:
        for line in Path("/proc/cpuinfo").read_text().splitlines():
            if line.startswith("model name"):
                cpu_model = line.split(":", 1)[1].strip()
                break
    except OSError:
        cpu_model = platform.processor() or None

    def out(*cmd: str) -> str | None:
        try:
            p = subprocess.run(cmd, capture_output=True, text=True)
            return (p.stdout or p.stderr).strip()
        except OSError:
            return None

    return {
        "platform": platform.platform(),
        "cpu_model": cpu_model,
        "cpu_count": os.cpu_count(),
        "server": f"nginx {srv.version}",
        "uv": out(uv, "--version"),
        "server_cpus": server_cpus,
        "client_cpus": client_cpus,
    }


# ---------------------------------------------------------------------------
# main run
# ---------------------------------------------------------------------------


def build_cases(args) -> list[dict]:
    cases = []
    for transport in args.transports:
        for scenario in args.scenarios:
            for client in args.clients:
                for proto in args.protocols:
                    if not catalog.supported(client, proto, transport):
                        continue
                    # Pythons innermost so that the versions being compared run
                    # back-to-back, minimising the effect of machine drift.
                    for py in args.pythons:
                        cases.append({"python": py, "transport": transport, "scenario": scenario,
                                      "client": client, "proto": proto})
    if args.shuffle:
        random.Random(args.seed).shuffle(cases)
    return cases


def run(args) -> int:
    uv = uv_bin(args.uv)
    workdir = Path(args.workdir).resolve()
    workdir.mkdir(parents=True, exist_ok=True)
    results_dir = Path(args.results_dir).resolve()
    results_dir.mkdir(parents=True, exist_ok=True)

    server_cpus = client_cpus = None
    ncpu = os.cpu_count() or 1
    if args.server_cpus or args.client_cpus:
        server_cpus = parse_cpus(args.server_cpus) if args.server_cpus else None
        client_cpus = parse_cpus(args.client_cpus) if args.client_cpus else None
    elif not args.no_pin and hasattr(os, "sched_setaffinity") and ncpu >= 4:
        half = ncpu // 2
        server_cpus, client_cpus = list(range(half)), list(range(half, ncpu))

    log("Preparing Python environments")
    interpreters: dict[str, Path] = {}
    envs: dict[str, dict] = {}
    for spec in args.pythons:
        interpreters[spec] = venv_python(workdir / "venvs" / spec) if args.no_install else \
            setup_env(spec, workdir, uv, not args.allow_system_python, args.reinstall)
        envs[spec] = env_info(interpreters[spec])
        pk = envs[spec]["packages"]
        log(f"  Python {envs[spec]['python']}: " + ", ".join(
            f"{d} {pk.get(d)}" for d in ("pycurl", "httpx2", "httpcore2", "urllib3", "requests", "aiohttp", "h2")))

    log("Starting nginx")
    srv = server.start(workdir, args.nginx, server_cpus)
    stamp = dt.datetime.now().strftime("%Y%m%d-%H%M%S")
    out_json = results_dir / f"bench-{stamp}.json"
    doc = {
        "created": dt.datetime.now().astimezone().isoformat(timespec="seconds"),
        "config": {
            "pythons": args.pythons, "transports": args.transports, "protocols": args.protocols,
            "clients": args.clients, "scenarios": args.scenarios, "rounds": args.rounds,
            "scale": args.scale, "concurrency": args.concurrency,
            "counts": {s: _count(s, args) for s in args.scenarios},
        },
        "machine": machine_info(srv, uv, server_cpus, client_cpus),
        "environments": envs,
        "cases": [],
    }
    cases = build_cases(args)
    log(f"Running {len(cases)} cases -> {out_json}")
    t_start = time.monotonic()
    try:
        for i, case in enumerate(cases, 1):
            result = run_case(case, args, interpreters[case["python"]], srv, client_cpus)
            doc["cases"].append({**case, **result})
            out_json.write_text(json.dumps(doc, indent=1))
            log(f"[{i:>4}/{len(cases)}] {_summary(case, result)}")
    finally:
        srv.stop()
    log(f"Done in {time.monotonic() - t_start:.0f}s")
    md, csv = report.write_all(out_json)
    log(f"Results: {out_json}\n         {csv}\n         {md}")
    return 0


def _count(scenario: str, args) -> int:
    base = catalog.SCENARIOS[scenario]["count"]
    n = max(1, round(base * args.scale))
    if scenario == "concurrent_small":
        n = max(n, args.concurrency)
    return n


def run_case(case: dict, args, py: Path, srv: server.Server, client_cpus) -> dict:
    n = _count(case["scenario"], args)
    spec = {
        **case,
        "base_url": srv.base_url(case["proto"], case["transport"]),
        "ca_file": str(srv.ca_file),
        "count": n,
        "rounds": args.rounds,
        "warmup": max(1, n // 5),
        "concurrency": args.concurrency,
        "client_cpus": client_cpus,
    }
    try:
        p = subprocess.run([str(py), str(WORKER), "run", json.dumps(spec)], capture_output=True,
                           text=True, timeout=args.timeout, cwd=HERE, env=worker_env())
    except subprocess.TimeoutExpired:
        return {"ok": False, "error": f"timed out after {args.timeout}s"}
    lines = p.stdout.strip().splitlines()
    try:
        res = json.loads(lines[-1])
    except (IndexError, json.JSONDecodeError):
        res = {"ok": False, "error": f"worker exit {p.returncode}: {p.stderr.strip()[-2000:]}"}
    if not res.get("ok") and args.verbose and res.get("traceback"):
        log(res["traceback"])
    return res


def _summary(case: dict, res: dict) -> str:
    head = f"py{case['python']:<5} {case['transport']:<5} {case['proto']} {case['client']:<16} {case['scenario']:<17}"
    if not res.get("ok"):
        return f"{head} FAILED: {res.get('error', '')[:160]}"
    m = report.metrics(case["scenario"], res)
    return f"{head} {m['throughput']:>10,.1f} {m['unit']:<5} ±{m['rsd_pct']:.1f}%  cpu {m['cpu_us_per_req']:,.0f} µs/req"


def main(argv: list[str]) -> int:
    if argv and argv[0] == "report":
        for f in argv[1:]:
            md, csv = report.write_all(Path(f))
            print(md)
            print(csv)
        return 0

    ap = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("--pythons", nargs="+", default=["3.12", "3.14", "3.15"],
                    help="uv Python request strings (e.g. 3.14, 3.14t, 3.15.0) [default: %(default)s]")
    ap.add_argument("--clients", nargs="+", default=list(catalog.CLIENTS), choices=list(catalog.CLIENTS))
    ap.add_argument("--protocols", nargs="+", default=list(catalog.PROTOCOLS), choices=catalog.PROTOCOLS)
    ap.add_argument("--transports", nargs="+", default=["tls"], choices=catalog.TRANSPORTS,
                    help="tls = HTTPS/ALPN (default); plain = cleartext h1 and h2c prior-knowledge")
    ap.add_argument("--scenarios", nargs="+", default=list(catalog.SCENARIOS), choices=list(catalog.SCENARIOS))
    ap.add_argument("--rounds", type=int, default=5, help="measured rounds per case [%(default)s]")
    ap.add_argument("--scale", type=float, default=1.0, help="multiply per-round request counts")
    ap.add_argument("--concurrency", type=int, default=32, help="in-flight requests for concurrent_small")
    ap.add_argument("--quick", action="store_true", help="smoke test: --scale 0.1 --rounds 2")
    ap.add_argument("--shuffle", action="store_true", help="randomise case order")
    ap.add_argument("--seed", type=int, default=0)
    ap.add_argument("--timeout", type=int, default=900, help="per-case timeout in seconds")
    ap.add_argument("--server-cpus", help="pin nginx to these CPUs, e.g. 0-1")
    ap.add_argument("--client-cpus", help="pin the client process to these CPUs, e.g. 2-3")
    ap.add_argument("--no-pin", action="store_true", help="disable automatic CPU split")
    ap.add_argument("--workdir", default=str(HERE / ".bench-work"))
    ap.add_argument("--results-dir", default=str(HERE / "results"))
    ap.add_argument("--nginx", default="nginx")
    ap.add_argument("--uv", default=None)
    ap.add_argument("--no-install", action="store_true", help="reuse existing venvs as-is")
    ap.add_argument("--reinstall", action="store_true", help="recreate venvs from scratch")
    ap.add_argument("--allow-system-python", action="store_true",
                    help="let uv pick a system interpreter instead of a uv-managed build")
    ap.add_argument("-v", "--verbose", action="store_true", help="print worker tracebacks")
    args = ap.parse_args(argv)
    if args.quick:
        args.scale, args.rounds = min(args.scale, 0.1), min(args.rounds, 2)
    return run(args)


if __name__ == "__main__":
    sys.exit(main(sys.argv[1:]))

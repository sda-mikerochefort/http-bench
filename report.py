"""Turn a results JSON file into Markdown and CSV summaries (stdlib only)."""

from __future__ import annotations

import csv
import math
import statistics
from pathlib import Path

import catalog

MiB = 1024 * 1024


def metrics(scenario: str, res: dict) -> dict:
    unit = catalog.SCENARIOS[scenario]["unit"]
    rounds = res["rounds"]
    if unit == "MiB/s":
        tps = [r["bytes"] / MiB / r["wall_s"] for r in rounds]
    else:
        tps = [r["requests"] / r["wall_s"] for r in rounds]
    cpu = [r["cpu_s"] / r["requests"] * 1e6 for r in rounds]
    med = statistics.median(tps)
    rsd = statistics.stdev(tps) / statistics.fmean(tps) * 100 if len(tps) > 1 else 0.0
    return {
        "unit": unit,
        "throughput": med,
        "throughput_min": min(tps),
        "throughput_max": max(tps),
        "rsd_pct": rsd,
        "cpu_us_per_req": statistics.median(cpu),
        "cpu_util": statistics.median(r["cpu_s"] / r["wall_s"] for r in rounds),
        **{k: v for k, v in res.get("latency", {}).items()},
        "maxrss_mib": res.get("maxrss_kib", 0) / 1024,
    }


def _fmt(v: float) -> str:
    if v >= 100:
        return f"{v:,.0f}"
    if v >= 10:
        return f"{v:.1f}"
    return f"{v:.2f}"


def _ratio(v: float) -> str:
    return f"{v:.2f}×"


def _geomean(xs: list[float]) -> float:
    return math.exp(sum(math.log(x) for x in xs) / len(xs)) if xs else float("nan")


def load(doc: dict):
    """Index ok cases as {(transport, scenario, client, proto, python): metrics}."""
    idx: dict[tuple, dict] = {}
    failures = []
    for c in doc["cases"]:
        key = (c["transport"], c["scenario"], c["client"], c["proto"], c["python"])
        if c.get("ok"):
            idx[key] = metrics(c["scenario"], c)
        else:
            failures.append(c)
    return idx, failures


def render_markdown(doc: dict) -> str:
    cfg = doc["config"]
    pys = cfg["pythons"]
    idx, failures = load(doc)
    failed = {(c["transport"], c["scenario"], c["client"], c["proto"], c["python"]) for c in failures}
    L: list[str] = []
    a = L.append

    a(f"# HTTP/1.1 vs HTTP/2 Python client benchmark\n")
    a(f"Run: {doc['created']}  ")
    m = doc["machine"]
    a(f"Machine: {m['cpu_model']} ({m['cpu_count']} CPUs), {m['platform']}  ")
    a(f"Server: {m['server']} on 127.0.0.1"
      + (f", pinned to CPUs {m['server_cpus']}; client pinned to CPUs {m['client_cpus']}" if m.get("client_cpus") else "")
      + "  ")
    a(f"Rounds per case: {cfg['rounds']} (median reported, ± = relative std-dev across rounds); "
      f"concurrency for `concurrent_small`: {cfg['concurrency']}  ")
    a("Requests per round: " + ", ".join(f"`{s}` {n}" for s, n in cfg["counts"].items()) + "\n")

    a("## Interpreters and library versions\n")
    envs = doc["environments"]
    a("| | " + " | ".join(f"Python {envs[p]['python']}" for p in pys) + " |")
    a("|---|" + "---|" * len(pys))
    a("| compiler | " + " | ".join(envs[p]["compiler"].strip() for p in pys) + " |")
    a("| OpenSSL (ssl module) | " + " | ".join(envs[p]["openssl"] for p in pys) + " |")
    a("| JIT | " + " | ".join(str(envs[p].get("jit")) for p in pys) + " |")
    for d in ("pycurl", "httpx2", "httpcore2", "urllib3", "requests", "aiohttp", "h2", "h11", "anyio"):
        a(f"| {d} | " + " | ".join(str(envs[p]["packages"].get(d)) for p in pys) + " |")
    a("| libcurl | " + " | ".join(envs[p].get("pycurl_version", "").split(" ")[1] if " " in envs[p].get("pycurl_version", "") else "?" for p in pys) + " |")
    a("| aiohttp C parser | " + " | ".join(str(envs[p].get("aiohttp_c_parser")) for p in pys) + " |")
    a("")

    for transport in cfg["transports"]:
        tname = "HTTPS (TLS, ALPN)" if transport == "tls" else "Cleartext (HTTP/1.1, h2c prior knowledge)"
        a(f"## {tname}\n")
        for scenario in cfg["scenarios"]:
            sc = catalog.SCENARIOS[scenario]
            rows = [(cl, pr) for cl in cfg["clients"] for pr in cfg["protocols"]
                    if catalog.supported(cl, pr, transport)]
            if not rows:
                continue
            a(f"### `{scenario}` — {sc['desc']}\n")
            a(f"Throughput, {sc['unit']} (higher is better). Best per column in **bold**.\n")
            a("| client | proto | " + " | ".join(pys) + " |")
            a("|---|---|" + "---:|" * len(pys))
            best = {}
            for p in pys:
                vals = [idx[(transport, scenario, cl, pr, p)]["throughput"] for cl, pr in rows
                        if (transport, scenario, cl, pr, p) in idx]
                best[p] = max(vals) if vals else None
            for cl, pr in rows:
                cells = []
                for p in pys:
                    k = (transport, scenario, cl, pr, p)
                    if k in idx:
                        v = idx[k]
                        s = f"{_fmt(v['throughput'])} ±{v['rsd_pct']:.0f}%"
                        cells.append(f"**{s}**" if v["throughput"] == best[p] else s)
                    elif k in failed:
                        cells.append("✗ failed")
                    else:
                        cells.append("–")
                a(f"| {cl} | {pr} | " + " | ".join(cells) + " |")
            a("")
            seq = scenario != "concurrent_small"
            hdr = "Client CPU time per request, µs (lower is better)"
            if seq:
                hdr += "; latency p50 / p99 in µs"
            a(f"<details><summary>{hdr}</summary>\n")
            a("| client | proto | " + " | ".join(pys) + " |")
            a("|---|---|" + "---:|" * len(pys))
            for cl, pr in rows:
                cells = []
                for p in pys:
                    v = idx.get((transport, scenario, cl, pr, p))
                    if not v:
                        cells.append("–")
                    elif seq and "p50_us" in v:
                        cells.append(f"cpu {_fmt(v['cpu_us_per_req'])} · {_fmt(v['p50_us'])} / {_fmt(v['p99_us'])}")
                    else:
                        cells.append(f"cpu {_fmt(v['cpu_us_per_req'])}")
                a(f"| {cl} | {pr} | " + " | ".join(cells) + " |")
            a("\n</details>\n")

        # --- HTTP/2 vs HTTP/1.1 ------------------------------------------------
        h2_clients = [cl for cl in cfg["clients"]
                      if catalog.supported(cl, "h1", transport) and catalog.supported(cl, "h2", transport)]
        if {"h1", "h2"} <= set(cfg["protocols"]) and h2_clients:
            a(f"### HTTP/2 ÷ HTTP/1.1 throughput ({tname})\n")
            a(">1.00× means HTTP/2 was faster for that client.\n")
            a("| client | scenario | " + " | ".join(pys) + " |")
            a("|---|---|" + "---:|" * len(pys))
            for cl in h2_clients:
                for scenario in cfg["scenarios"]:
                    cells = []
                    for p in pys:
                        v1 = idx.get((transport, scenario, cl, "h1", p))
                        v2 = idx.get((transport, scenario, cl, "h2", p))
                        cells.append(_ratio(v2["throughput"] / v1["throughput"]) if v1 and v2 else "–")
                    a(f"| {cl} | {scenario} | " + " | ".join(cells) + " |")
            a("")

        # --- Python versions ---------------------------------------------------
        if len(pys) > 1:
            base = pys[0]
            a(f"### Python version speed-up vs {base} ({tname})\n")
            a(f"Geometric mean over scenarios of throughput ÷ Python {base} throughput "
              "(only scenarios that succeeded on both).\n")
            a("| client | proto | " + " | ".join(f"{p} vs {base}" for p in pys[1:]) + " |")
            a("|---|---|" + "---:|" * (len(pys) - 1))
            for cl in cfg["clients"]:
                for pr in cfg["protocols"]:
                    if not catalog.supported(cl, pr, transport):
                        continue
                    cells = []
                    for p in pys[1:]:
                        rs = [idx[(transport, s, cl, pr, p)]["throughput"] / idx[(transport, s, cl, pr, base)]["throughput"]
                              for s in cfg["scenarios"]
                              if (transport, s, cl, pr, p) in idx and (transport, s, cl, pr, base) in idx]
                        cells.append(_ratio(_geomean(rs)) if rs else "–")
                    a(f"| {cl} | {pr} | " + " | ".join(cells) + " |")
            a("")

    if failures:
        a("## Failed cases\n")
        a("| python | transport | client | proto | scenario | error |")
        a("|---|---|---|---|---|---|")
        for c in failures:
            err = c.get("error", "").replace("|", "\\|").replace("\n", " ")[:200]
            a(f"| {c['python']} | {c['transport']} | {c['client']} | {c['proto']} | {c['scenario']} | {err} |")
        a("")

    a("## Client notes\n")
    for cl in cfg["clients"]:
        a(f"- **{cl}** — {catalog.CLIENTS[cl]['notes']}")
    a("")
    return "\n".join(L)


CSV_FIELDS = [
    "python", "python_full", "transport", "scenario", "client", "proto", "ok", "unit", "throughput",
    "throughput_min", "throughput_max", "rsd_pct", "cpu_us_per_req", "cpu_util", "p50_us", "p90_us",
    "p99_us", "max_us", "maxrss_mib", "library_version", "error",
]


def write_all(json_path: Path) -> tuple[Path, Path]:
    import json

    doc = json.loads(Path(json_path).read_text())
    md_path = Path(json_path).with_suffix(".md")
    csv_path = Path(json_path).with_suffix(".csv")
    md_path.write_text(render_markdown(doc))
    envs = doc["environments"]
    with csv_path.open("w", newline="") as f:
        w = csv.DictWriter(f, fieldnames=CSV_FIELDS, extrasaction="ignore")
        w.writeheader()
        for c in doc["cases"]:
            row = {k: c.get(k) for k in ("python", "transport", "scenario", "client", "proto", "ok", "error")}
            env = envs.get(c["python"], {})
            row["python_full"] = env.get("python")
            row["library_version"] = env.get("packages", {}).get(catalog.CLIENTS[c["client"]]["dist"])
            if c.get("ok"):
                row.update({k: (round(v, 3) if isinstance(v, float) else v)
                            for k, v in metrics(c["scenario"], c).items()})
            w.writerow(row)
    return md_path, csv_path


if __name__ == "__main__":
    import sys

    for p in sys.argv[1:]:
        print(*write_all(Path(p)), sep="\n")

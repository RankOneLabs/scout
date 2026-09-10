"""Format pooled relevance sweep rows into the publishing table (markdown)."""
from __future__ import annotations
import re, sys
from collections import defaultdict

FIT = {
    "qwen3-30b-a3b-2507": "48GB", "gpt-oss-20b": "48GB", "gemma-4-26b-a4b": "48GB", "gemma-4-31b": "48GB",
    "mistral-small-2603": "48GB", "nemotron-3-nano-30b": "48GB", "qwen3-32b": "48GB",
    "llama-3-3-70b": "48GB*", "qwen3-next-80b-a3b": "48GB*",
    "gpt-oss-120b": "96GB", "glm-4-5-air": "96GB", "glm-4-7-flash": "96GB",
    "nemotron-3-super-120b": "96GB", "llama-4-scout": "96GB",
    "qwen3-235b-2507": "API", "kimi-k2-0905": "API",
}
ORDER = ["48GB", "48GB*", "96GB", "API"]

def parse(path):
    rows = []
    for line in open(path):
        m = re.match(r"\s*(\d+)\s+(\S+)\s+(\S+)\s+(\S+)\s+(reused|override)\s+(\d+)\s+(\d+)\s+(\d+)\s+(\d+)\s+(\d+)\s+([\d.]+)\s+\|\s+(\d+)/(\d+)/(\d+)(?:\s+\|\s+([\d.]+)\s+([\d.]+)\s+(\d+))?", line)
        if m:
            run, proj, sweep, var, prm, n, ok, fp, fn, fail, usd, bok, bfp, bfn, med, p95, tok = m.groups()
            rows.append(dict(run=int(run), proj=proj, sweep=sweep, var=var, prompt=prm, n=int(n), ok=int(ok), fp=int(fp), fn=int(fn), fail=int(fail), usd=float(usd), bok=int(bok), bfp=int(bfp), bfn=int(bfn),
                             med=float(med) if med else None, p95=float(p95) if p95 else None, tok=int(tok) if tok else None))
    return rows

FRINK = {  # frink variant -> (label, OpenRouter counterpart); llama rows dropped, see RESULTS notes
    "qwen3-30b-a3b-q4-frink": ("qwen3-30b-a3b-2507 Q4_K_M", "qwen3-30b-a3b-2507"),
    "gemma-4-26b-a4b-q4-nothink-frink": ("gemma-4-26b-a4b Q4, thinking off", "gemma-4-26b-a4b"),
    "gemma-4-31b-q4-nothink-frink": ("gemma-4-31b Q4, thinking off", "gemma-4-31b"),
    "gemma-4-26b-a4b-q4-frink": ("gemma-4-26b-a4b Q4, thinking ON (Ollama default)", "gemma-4-26b-a4b"),
    "gemma-4-31b-q4-frink": ("gemma-4-31b Q4, thinking ON (Ollama default)", "gemma-4-31b"),
}

def variant_key(r):
    if "prompt-agent" in r["sweep"]:
        model = "gemini-2-5-flash" if "gemini" in r["sweep"] else "qwen3-235b-2507"
        return model, r["var"]  # var = no-reject | topical
    if "open-models-bounded" in r["sweep"]:
        return r["var"], "current"
    if "-topical" in r["sweep"]:
        return r["var"], "topical"
    return r["var"], "current"

def cell(r):
    if r is None:
        return "—"
    s = f"{r['ok']}/{r['n']} (FP{r['fp']} FN{r['fn']}"
    if r["fail"]:
        s += f" fail{r['fail']}"
    return s + ")"

def lat(r):
    """median / p95 seconds per case for the ops-topical cell, or —."""
    if r is None or r.get("med") is None:
        return "—"
    return f"{r['med']:.1f} / {r['p95']:.1f}"

def latest(rows):
    """Keep the latest run per (proj, model, prompt) among rows, preferring fewest failures."""
    best = {}
    for r in rows:
        k = (r["proj"],) + variant_key(r)
        if k not in best or (r["fail"], -r["run"]) < (best[k]["fail"], -best[k]["run"]):
            best[k] = r
    return best

def load(all_path="pool_all.txt", exgaia_path="pool_exgaia.txt"):
    """(ops latest map, evals latest map, all ops rows, all evals rows)."""
    ops_rows = [r for r in parse(all_path) if r["proj"] == "agent-ops"]
    ev_rows = [r for r in parse(exgaia_path) if r["proj"] == "agent-evals"]
    return latest(ops_rows), latest(ev_rows), ops_rows, ev_rows


if __name__ == "__main__":
    ops = latest([r for r in parse("pool_all.txt") if r["proj"] == "agent-ops"])
    ev = latest([r for r in parse("pool_exgaia.txt") if r["proj"] == "agent-evals"])
    b_ops = next(iter(ops.values())); b_ev = next(iter(ev.values()))

    print("### Prompt grid (frontier / large API models)\n")
    print("| model | ops current | ops no-reject | ops topical | evals current | evals no-reject | evals topical |")
    print("|---|---|---|---|---|---|---|")
    g = lambda d, p, m, v: d.get((p, m, v))
    print(f"| gemini-2.5-flash (baseline) | {b_ops['bok']}/51 (FP{b_ops['bfp']} FN{b_ops['bfn']}) | {cell(g(ops,'agent-ops','gemini-2-5-flash','no-reject'))} | {cell(g(ops,'agent-ops','gemini-2-5-flash','topical'))} | {b_ev['bok']}/28 (FP{b_ev['bfp']} FN{b_ev['bfn']}) | {cell(g(ev,'agent-evals','gemini-2-5-flash','no-reject'))} | {cell(g(ev,'agent-evals','gemini-2-5-flash','topical'))} |")
    for m in ("qwen3-235b-2507", "kimi-k2-0905"):
        print(f"| {m} | {cell(g(ops,'agent-ops',m,'current'))} | {cell(g(ops,'agent-ops',m,'no-reject'))} | {cell(g(ops,'agent-ops',m,'topical'))} | {cell(g(ev,'agent-evals',m,'current'))} | {cell(g(ev,'agent-evals',m,'no-reject'))} | {cell(g(ev,'agent-evals',m,'topical'))} |")

    print("\n### Local-class models (OpenRouter full precision = ceiling)\n")
    print("| fit | model | ops current | ops topical | evals current | evals topical | usd/4 runs | s/case med / p95 (ops topical) |")
    print("|---|---|---|---|---|---|---|---|")
    models = [m for m in FIT if FIT[m] != "API"]
    for fit in ORDER[:-1]:
        for m in [x for x in models if FIT[x] == fit]:
            rs = [g(ops,'agent-ops',m,'current'), g(ops,'agent-ops',m,'topical'), g(ev,'agent-evals',m,'current'), g(ev,'agent-evals',m,'topical')]
            usd = sum(r["usd"] for r in rs if r)
            # evals usd is ex-GAIA subset; recompute from all file for cost
            print(f"| {fit} | {m} | " + " | ".join(cell(r) for r in rs) + f" | {usd:.3f} | {lat(rs[1])} |")

    print("\n### True-local on frink (Ollama, quantised) vs OpenRouter ceiling\n")
    print("| frink model | ops current | ops topical | evals current | evals topical | s/case med / p95 (ops topical) | OpenRouter: ops current | ops topical | evals current | evals topical | s/case (ops topical) |")
    print("|---|---|---|---|---|---|---|---|---|---|---|")
    for v, (label, ceil) in FRINK.items():
        local = [g(ops,'agent-ops',v,'current'), g(ops,'agent-ops',v,'topical'), g(ev,'agent-evals',v,'current'), g(ev,'agent-evals',v,'topical')]
        api = [g(ops,'agent-ops',ceil,'current'), g(ops,'agent-ops',ceil,'topical'), g(ev,'agent-evals',ceil,'current'), g(ev,'agent-evals',ceil,'topical')]
        if not any(local):
            continue
        print(f"| {label} | " + " | ".join(cell(r) for r in local) + f" | {lat(local[1])} | " + " | ".join(cell(r) for r in api) + f" | {lat(api[1])} |")

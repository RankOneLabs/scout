"""Turn the pooled relevance rows into the RESULTS.md tables and the chart page.

    python scripts/format_report.py                 # markdown tables to stdout
    python scripts/format_report.py --charts charts.html

Reads `pooled-all.txt` and `pooled-exclude-gaia.txt` from the evidence
directory (the parent of this script's directory). agent-evals cells come
from the GAIA-excluded file; costs always come from the complete file.
"""

from __future__ import annotations

import argparse
import json
import re
from collections import defaultdict
from collections.abc import Iterable, Mapping, Sequence
from dataclasses import dataclass
from datetime import date
from pathlib import Path

EVIDENCE_DIR = Path(__file__).resolve().parent.parent
POOLED_ALL = EVIDENCE_DIR / "pooled-all.txt"
POOLED_EXCLUDE_GAIA = EVIDENCE_DIR / "pooled-exclude-gaia.txt"
CHART_TEMPLATE = Path(__file__).resolve().parent / "charts.template.html"

ROW_PATTERN = re.compile(
    r"\s*(\d+)\s+(\S+)\s+(\S+)\s+(\S+)\s+(reused|override)\s+(\d+)\s+(\d+)\s+(\d+)\s+(\d+)"
    r"\s+(\d+)\s+([\d.]+)\s+\|\s+(\d+)/(\d+)/(\d+)(?:\s+\|\s+([\d.]+)\s+([\d.]+)\s+(\d+))?"
)

# VRAM class of each hosted local-class model at its native precision;
# "48GB*" fits 48GB only at 4-bit.
FIT: Mapping[str, str] = {
    "qwen3-30b-a3b-2507": "48GB",
    "gpt-oss-20b": "48GB",
    "gemma-4-26b-a4b": "48GB",
    "gemma-4-31b": "48GB",
    "mistral-small-2603": "48GB",
    "nemotron-3-nano-30b": "48GB",
    "qwen3-32b": "48GB",
    "llama-3-3-70b": "48GB*",
    "qwen3-next-80b-a3b": "48GB*",
    "gpt-oss-120b": "96GB",
    "glm-4-5-air": "96GB",
    "glm-4-7-flash": "96GB",
    "nemotron-3-super-120b": "96GB",
    "llama-4-scout": "96GB",
    "qwen3-235b-2507": "API",
    "kimi-k2-0905": "API",
}
FIT_ORDER = ("48GB", "48GB*", "96GB")
FRONTIER = ("qwen3-235b-2507", "kimi-k2-0905")

# Legacy frink variant name -> (label, hosted counterpart). The recorded runs
# predate study.yaml; runs expanded from the grid carry these attributes in
# manifest.json instead. llama rows are dropped (see RESULTS.md notes).
FRINK: Mapping[str, tuple[str, str]] = {
    "qwen3-30b-a3b-q4-frink": ("qwen3-30b-a3b-2507 Q4_K_M", "qwen3-30b-a3b-2507"),
    "gemma-4-26b-a4b-q4-nothink-frink": ("gemma-4-26b-a4b Q4, thinking off", "gemma-4-26b-a4b"),
    "gemma-4-31b-q4-nothink-frink": ("gemma-4-31b Q4, thinking off", "gemma-4-31b"),
    "gemma-4-26b-a4b-q4-frink": (
        "gemma-4-26b-a4b Q4, thinking ON (Ollama default)",
        "gemma-4-26b-a4b",
    ),
    "gemma-4-31b-q4-frink": ("gemma-4-31b Q4, thinking ON (Ollama default)", "gemma-4-31b"),
}

CHART_LABELS: Mapping[str, str] = {
    "gemini-2-5-flash": "gemini-2.5-flash",
    "qwen3-30b-a3b-2507": "qwen3-30b-a3b",
    "llama-3-3-70b": "llama-3.3-70b",
    "glm-4-5-air": "glm-4.5-air",
    "glm-4-7-flash": "glm-4.7-flash",
    "qwen3-30b-a3b-q4-frink": "qwen3-30b-a3b Q4 · frink",
    "gemma-4-26b-a4b-q4-nothink-frink": "gemma-4-26b-a4b Q4 · frink",
    "gemma-4-31b-q4-nothink-frink": "gemma-4-31b Q4 · frink",
    "gemma-4-26b-a4b-q4-frink": "gemma-4-26b-a4b Q4 +thinking · frink",
    "gemma-4-31b-q4-frink": "gemma-4-31b Q4 +thinking · frink",
}
HOSTED_ORDER = ("gemini-2-5-flash", *FRONTIER, *(m for m in FIT if FIT[m] != "API"))
UNUSABLE: Mapping[str, str] = {
    "nemotron-3-super-120b": "rate-limited upstream on every attempt",
}
THINKING_ON = ("gemma-4-26b-a4b-q4-frink", "gemma-4-31b-q4-frink")

PROMPTS = ("current", "no-reject", "topical")
CASE_COUNT = {"agent-ops": 51, "agent-evals": 28}


@dataclass(frozen=True, slots=True)
class Row:
    run: int
    proj: str
    sweep: str
    var: str
    prompt: str
    n: int
    ok: int
    fp: int
    fn: int
    fail: int
    usd: float
    bok: int
    bfp: int
    bfn: int
    med: float | None
    p95: float | None
    tok: int | None


Key = tuple[str, str, str]  # (project, model, prompt)


def parse(path: Path) -> list[Row]:
    rows: list[Row] = []
    for line in path.read_text(encoding="utf-8").splitlines():
        m = ROW_PATTERN.match(line)
        if m is None:
            continue
        g = m.groups()
        rows.append(
            Row(
                run=int(g[0]),
                proj=g[1],
                sweep=g[2],
                var=g[3],
                prompt=g[4],
                n=int(g[5]),
                ok=int(g[6]),
                fp=int(g[7]),
                fn=int(g[8]),
                fail=int(g[9]),
                usd=float(g[10]),
                bok=int(g[11]),
                bfp=int(g[12]),
                bfn=int(g[13]),
                med=float(g[14]) if g[14] else None,
                p95=float(g[15]) if g[15] else None,
                tok=int(g[16]) if g[16] else None,
            )
        )
    return rows


def variant_key(row: Row) -> tuple[str, str]:
    """(model, prompt) for a legacy run name; grid-expanded runs carry these
    attributes in manifest.json instead of in their names."""
    if "prompt-agent" in row.sweep:
        model = "gemini-2-5-flash" if "gemini" in row.sweep else "qwen3-235b-2507"
        return model, row.var  # variant is the prompt name here
    if "open-models-bounded" in row.sweep:
        return row.var, "current"
    if "-topical" in row.sweep:
        return row.var, "topical"
    return row.var, "current"


def latest(rows: Iterable[Row]) -> dict[Key, Row]:
    """One row per (project, model, prompt): fewest failures, then latest run."""
    best: dict[Key, Row] = {}
    for r in rows:
        key: Key = (r.proj, *variant_key(r))
        current = best.get(key)
        if current is None or (r.fail, -r.run) < (current.fail, -current.run):
            best[key] = r
    return best


@dataclass(frozen=True, slots=True)
class Pools:
    ops: dict[Key, Row]
    evals: dict[Key, Row]
    ops_rows: list[Row]
    evals_rows: list[Row]
    cost_by_run: dict[int, float]

    def cell(self, proj: str, model: str, prompt: str) -> Row | None:
        table = self.ops if proj == "agent-ops" else self.evals
        return table.get((proj, model, prompt))

    def cost(self, row: Row | None) -> float:
        """Cost of a run from the complete pooled file, so agent-evals costs
        cover all 73 cases and not only the GAIA-excluded 28."""
        return 0.0 if row is None else self.cost_by_run.get(row.run, row.usd)


def load(all_path: Path = POOLED_ALL, exgaia_path: Path = POOLED_EXCLUDE_GAIA) -> Pools:
    all_rows = parse(all_path)
    exgaia_rows = parse(exgaia_path)
    ops_rows = [r for r in all_rows if r.proj == "agent-ops"]
    evals_rows = [r for r in exgaia_rows if r.proj == "agent-evals"]
    return Pools(
        ops=latest(ops_rows),
        evals=latest(evals_rows),
        ops_rows=ops_rows,
        evals_rows=evals_rows,
        cost_by_run={r.run: r.usd for r in all_rows},
    )


def cell(row: Row | None) -> str:
    if row is None:
        return "—"
    text = f"{row.ok}/{row.n} (FP{row.fp} FN{row.fn}"
    if row.fail:
        text += f" fail{row.fail}"
    return text + ")"


def latency(row: Row | None) -> str:
    if row is None or row.med is None or row.p95 is None:
        return "—"
    return f"{row.med:.1f} / {row.p95:.1f}"


def baseline_cell(row: Row, n: int) -> str:
    return f"{row.bok}/{n} (FP{row.bfp} FN{row.bfn})"


def four_cells(pools: Pools, model: str) -> list[Row | None]:
    return [
        pools.cell("agent-ops", model, "current"),
        pools.cell("agent-ops", model, "topical"),
        pools.cell("agent-evals", model, "current"),
        pools.cell("agent-evals", model, "topical"),
    ]


def tables(pools: Pools) -> str:
    b_ops = next(iter(pools.ops.values()))
    b_ev = next(iter(pools.evals.values()))
    out: list[str] = []
    out.append("### Prompt grid (frontier / large hosted models)\n")
    out.append(
        "| model | ops current | ops no-reject | ops topical "
        "| evals current | evals no-reject | evals topical |"
    )
    out.append("|---|---|---|---|---|---|---|")
    gemini = [pools.cell(p, "gemini-2-5-flash", v) for p in CASE_COUNT for v in PROMPTS[1:]]
    out.append(
        f"| gemini-2.5-flash (baseline) | {baseline_cell(b_ops, 51)} | {cell(gemini[0])} "
        f"| {cell(gemini[1])} | {baseline_cell(b_ev, 28)} | {cell(gemini[2])} | {cell(gemini[3])} |"
    )
    for model in FRONTIER:
        cells = [pools.cell(p, model, v) for p in CASE_COUNT for v in PROMPTS]
        out.append(f"| {model} | " + " | ".join(cell(c) for c in cells) + " |")

    out.append("\n### Local-class models (OpenRouter hosted run; provider precision)\n")
    out.append(
        "| fit | model | ops current | ops topical | evals current | evals topical "
        "| usd/4 runs | s/case med / p95 (ops topical) |"
    )
    out.append("|---|---|---|---|---|---|---|---|")
    for fit in FIT_ORDER:
        for model in (m for m in FIT if FIT[m] == fit):
            cells = four_cells(pools, model)
            usd = sum(pools.cost(c) for c in cells)
            out.append(
                f"| {fit} | {model} | "
                + " | ".join(cell(c) for c in cells)
                + f" | {usd:.3f} | {latency(cells[1])} |"
            )

    out.append("\n### True-local on frink (Ollama, quantised) vs the OpenRouter hosted run\n")
    out.append(
        "| frink model | ops current | ops topical | evals current | evals topical "
        "| s/case med / p95 (ops topical) | OpenRouter: ops current | ops topical "
        "| evals current | evals topical | s/case (ops topical) |"
    )
    out.append("|---|---|---|---|---|---|---|---|---|---|---|")
    for variant, (label, hosted) in FRINK.items():
        local = four_cells(pools, variant)
        if not any(local):
            continue
        api = four_cells(pools, hosted)
        out.append(
            f"| {label} | "
            + " | ".join(cell(c) for c in local)
            + f" | {latency(local[1])} | "
            + " | ".join(cell(c) for c in api)
            + f" | {latency(api[1])} |"
        )
    return "\n".join(out) + "\n"


def _cell_json(row: Row | None) -> dict[str, object] | None:
    if row is None:
        return None
    return {
        "run": row.run,
        "ok": row.ok,
        "n": row.n,
        "fp": row.fp,
        "fn": row.fn,
        "fail": row.fail,
        "med": row.med,
        "p95": row.p95,
        "tok": row.tok,
    }


def chart_data(pools: Pools) -> dict[str, object]:
    """The JSON the chart template renders from; everything derives from the
    pooled rows."""
    b_ops = next(iter(pools.ops.values()))
    b_ev = next(iter(pools.evals.values()))
    baseline = {
        "ops": {"ok": b_ops.bok, "n": 51, "fp": b_ops.bfp, "fn": b_ops.bfn},
        "evals": {"ok": b_ev.bok, "n": 28, "fp": b_ev.bfp, "fn": b_ev.bfn},
    }
    models: list[dict[str, object]] = []
    for model in (*HOSTED_ORDER, *FRINK):
        where = "frink" if model.endswith("-frink") else "hosted"
        cells: dict[str, dict[str, object] | None] = {
            f"{proj.removeprefix('agent-')}_{prompt.replace('-', '')}": _cell_json(
                pools.cell(proj, model, prompt)
            )
            for proj in CASE_COUNT
            for prompt in PROMPTS
        }
        if model == "gemini-2-5-flash":  # its recorded-prompt cells are the baseline itself
            cells["ops_current"] = dict(baseline["ops"], fail=0, med=None, p95=None, tok=None)
            cells["evals_current"] = dict(baseline["evals"], fail=0, med=None, p95=None, tok=None)
        if not any(cells.values()):
            continue
        models.append(
            {
                "id": model,
                "label": CHART_LABELS.get(model, model),
                "where": where,
                "fit": "frink" if where == "frink" else FIT.get(model, "API"),
                "hosted_of": FRINK[model][1] if model in FRINK else None,
                "unusable": UNUSABLE.get(model),
                "thinking": model in THINKING_ON,
                "cells": cells,
            }
        )
    groups: dict[Key, list[Row]] = defaultdict(list)
    for r in (*pools.ops_rows, *pools.evals_rows):
        groups[(r.proj, *variant_key(r))].append(r)
    repeats = [
        {
            "label": CHART_LABELS.get(model, model),
            "proj": proj.removeprefix("agent-"),
            "prompt": prompt,
            "n": clean[0].n,
            "draws": sorted(r.ok for r in clean),
        }
        for (proj, model, prompt), rows in groups.items()
        if len(clean := [r for r in rows if r.fail == 0]) >= 2
    ]
    return {
        "generated": date.today().isoformat(),
        "baseline": baseline,
        "models": models,
        "repeats": repeats,
    }


def render_charts(pools: Pools, template: Path = CHART_TEMPLATE) -> str:
    data = json.dumps(chart_data(pools)).replace("</", "<\\/")
    return template.read_text(encoding="utf-8").replace("__DATA__", data)


def repeat_draws(pools: Pools) -> dict[Key, list[int]]:
    """Every configuration drawn more than once without failures, with its
    scores in run order. RESULTS.md quotes these."""
    groups: dict[Key, list[Row]] = defaultdict(list)
    for r in (*pools.ops_rows, *pools.evals_rows):
        if r.fail == 0:
            groups[(r.proj, *variant_key(r))].append(r)
    return {
        key: [r.ok for r in sorted(rows, key=lambda r: r.run)]
        for key, rows in groups.items()
        if len(rows) >= 2
    }


def main(argv: Sequence[str] | None = None) -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--charts", metavar="OUT", help="write the chart page to OUT")
    parser.add_argument("--repeats", action="store_true", help="print repeat-draw scores")
    args = parser.parse_args(argv)
    pools = load()
    if args.charts:
        html = render_charts(pools)
        Path(args.charts).write_text(html, encoding="utf-8")
        print(f"{args.charts}: {len(html)} bytes")
    elif args.repeats:
        for key, draws in sorted(repeat_draws(pools).items()):
            print(key, draws)
    else:
        print(tables(pools), end="")


if __name__ == "__main__":
    main()

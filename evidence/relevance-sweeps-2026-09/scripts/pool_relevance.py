"""Pool relevance replay runs into one text table, one row per experiment run.

Runs inside the scout container against its SQLite files, e.g.

    ssh willie 'docker exec -i engagement-scout uv run --no-sync python - 24' \\
        < scripts/pool_relevance.py > pooled-all.txt
    ... python - --exclude-route GAIA 24' < ... > pooled-exclude-gaia.txt

Columns: run, project, sweep, variant, prompt (reused|override), n, ok, FP, FN,
fail, usd | baseline ok/FP/FN | median s/case, p95 s/case, mean output tokens.
"""

from __future__ import annotations

import json
import sqlite3
import statistics
import sys
from collections import Counter
from collections.abc import Sequence
from dataclasses import dataclass

DB_PATH = "/app/data/scout.db"
TRACES_PATH = "/app/data/scout_traces.db"


@dataclass(frozen=True, slots=True)
class RunRow:
    run: int
    project: str
    sweep: str
    variant: str
    model: str
    prompt: str
    n: int
    ok: int
    fp: int
    fn: int
    fail: int
    usd: float
    baseline_ok: int
    baseline_fp: int
    baseline_fn: int
    median_s: float
    p95_s: float
    out_tokens: float


def parse_args(argv: Sequence[str]) -> tuple[str | None, int]:
    args = list(argv)
    exclude_route: str | None = None
    if "--exclude-route" in args:
        i = args.index("--exclude-route")
        exclude_route = args[i + 1]
        del args[i : i + 2]
    run_min = int(args[0]) if args else 14
    return exclude_route, run_min


def keyword_route(conn: sqlite3.Connection, evaluation_id: int) -> str | None:
    row = conn.execute(
        "select k.keyword from evaluations e "
        "left join project_keywords k on k.id = e.keyword_route_id where e.id = ?",
        (evaluation_id,),
    ).fetchone()
    return None if row is None else row[0]


def pool_run(
    conn: sqlite3.Connection,
    traces: sqlite3.Connection,
    run_id: int,
    name: str,
    exclude_route: str | None,
) -> RunRow | None:
    n = tp = fp = tn = fn = failed = 0
    cost = 0.0
    baseline: Counter[str] = Counter()
    model = ""
    prompt_reused = False
    durations: list[float] = []
    out_tokens: list[int] = []
    experiments = conn.execute(
        "select candidate_trace_id, baseline_evidence, status, candidate_cost "
        "from evaluation_experiments where experiment_run_id = ?",
        (run_id,),
    )
    for trace_id, evidence_json, status, candidate_cost in experiments:
        evidence = json.loads(evidence_json)
        target = evidence["target"]
        evaluation_id = int(target["evaluation_id"])
        if exclude_route and keyword_route(conn, evaluation_id) == exclude_route:
            continue
        model = evidence["candidate_model"]
        prompt_reused = bool(evidence["baseline_prompt_reused"])
        is_relevant = bool(target["is_relevant"])
        baseline_relevant = bool(
            conn.execute(
                "select relevant from evaluations where id = ?", (evaluation_id,)
            ).fetchone()[0]
        )
        if baseline_relevant == is_relevant:
            baseline["ok"] += 1
        elif baseline_relevant:
            baseline["fp"] += 1
        else:
            baseline["fn"] += 1
        if status != "running":
            n += 1
        if status != "complete":
            failed += status == "failed"
            continue
        cost += candidate_cost or 0.0
        span = traces.execute(
            "select output, duration_ms from spans where trace_id = ? and kind = 'agent_run'",
            (trace_id,),
        ).fetchone()
        correct = json.loads(span[0])["scores"][0]["value"] == 1.0
        if span[1] is not None:
            durations.append(float(span[1]) / 1000)
        tokens = traces.execute(
            "select coalesce(sum(usage_output_tokens), 0) from spans "
            "where trace_id = ? and kind = 'llm_call'",
            (trace_id,),
        ).fetchone()
        out_tokens.append(int(tokens[0]))
        if correct:
            tp += is_relevant
            tn += not is_relevant
        else:
            fn += is_relevant
            fp += not is_relevant
    if n == 0:
        return None
    durations.sort()
    sweep, _, variant = name.rpartition(":")
    return RunRow(
        run=run_id,
        project="agent-evals" if "agent-evals" in name else "agent-ops",
        sweep=sweep,
        variant=variant,
        model=model,
        prompt="reused" if prompt_reused else "override",
        n=n,
        ok=tp + tn,
        fp=fp,
        fn=fn,
        fail=failed,
        usd=cost,
        baseline_ok=baseline["ok"],
        baseline_fp=baseline["fp"],
        baseline_fn=baseline["fn"],
        median_s=statistics.median(durations) if durations else 0.0,
        p95_s=durations[int(0.95 * (len(durations) - 1))] if durations else 0.0,
        out_tokens=statistics.mean(out_tokens) if out_tokens else 0.0,
    )


def format_rows(rows: Sequence[RunRow]) -> str:
    header = (
        f"{'run':>3} {'project':11} {'sweep':48} {'variant':30} {'prompt':8} {'n':>3} {'ok':>3} "
        f"{'FP':>3} {'FN':>3} {'fail':>4} {'usd':>8} | baseline ok/FP/FN | med_s p95_s out_tok"
    )
    lines = [header]
    for r in rows:
        lines.append(
            f"{r.run:>3} {r.project:11} {r.sweep[:48]:48} {r.variant:30} {r.prompt:8} {r.n:>3} "
            f"{r.ok:>3} {r.fp:>3} {r.fn:>3} {r.fail:>4} {r.usd:>8.4f} | "
            f"{r.baseline_ok}/{r.baseline_fp}/{r.baseline_fn} | "
            f"{r.median_s:.1f} {r.p95_s:.1f} {r.out_tokens:.0f}"
        )
    return "\n".join(lines)


def main(argv: Sequence[str]) -> None:
    exclude_route, run_min = parse_args(argv)
    conn = sqlite3.connect(DB_PATH)
    traces = sqlite3.connect(TRACES_PATH)
    rows: list[RunRow] = []
    for run_id, name in conn.execute(
        "select id, name from experiment_runs where id >= ? order by id", (run_min,)
    ):
        row = pool_run(conn, traces, int(run_id), str(name), exclude_route)
        if row is not None:
            rows.append(row)
    print(format_rows(rows))


if __name__ == "__main__":
    main(sys.argv[1:])

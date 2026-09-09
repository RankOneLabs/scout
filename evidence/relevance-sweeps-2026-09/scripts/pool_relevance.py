"""Pool relevance replay runs into one table. Usage: python - [--exclude-route GAIA] [run_id_min]"""
import sqlite3, json, sys, collections
args = sys.argv[1:]
exclude_kw = None
if "--exclude-route" in args:
    i = args.index("--exclude-route"); exclude_kw = args[i+1]; del args[i:i+2]
run_min = int(args[0]) if args else 14
c = sqlite3.connect("/app/data/scout.db"); t = sqlite3.connect("/app/data/scout_traces.db")
route_of = {}
def route(evid):
    if evid not in route_of:
        r = c.execute("select k.keyword from evaluations e left join project_keywords k on k.id=e.keyword_route_id where e.id=?", (evid,)).fetchone()
        route_of[evid] = r[0] if r else None
    return route_of[evid]
rows = []
for rid, name, status in c.execute("select id, name, status from experiment_runs where id>=? order by id", (run_min,)):
    n = tp = fp = tn = fn = failed = 0; cost = 0.0; base = collections.Counter(); model = None; prompt_reused = None
    for ctid, bev, st, ccost in c.execute("select candidate_trace_id, baseline_evidence, status, candidate_cost from evaluation_experiments where experiment_run_id=?", (rid,)):
        ev = json.loads(bev); tgt = ev["target"]; evid = tgt["evaluation_id"]
        if exclude_kw and route(evid) == exclude_kw: continue
        model = ev["candidate_model"]; prompt_reused = ev["baseline_prompt_reused"]
        brel = c.execute("select relevant from evaluations where id=?", (evid,)).fetchone()[0]
        base["ok" if bool(brel) == tgt["is_relevant"] else ("fp" if brel else "fn")] += 1
        n += (st != 'running')
        if st != "complete":
            failed += (st == "failed"); continue
        cost += ccost or 0
        r = t.execute("select output from spans where trace_id=? and kind='agent_run'", (ctid,)).fetchone()
        o = json.loads(r[0]); ok = o["scores"][0]["value"] == 1.0
        if ok: tp += tgt["is_relevant"]; tn += (not tgt["is_relevant"])
        else: fn += tgt["is_relevant"]; fp += (not tgt["is_relevant"])
    if n == 0: continue
    proj = "agent-evals" if "agent-evals" in name else "agent-ops"
    sweep, _, variant = name.rpartition(":")
    rows.append((rid, proj, sweep, variant, model, "reused" if prompt_reused else "override", n, tp+tn, fp, fn, failed, cost, base["ok"], base["fp"], base["fn"]))
print(f"{'run':>3} {'project':11} {'sweep':48} {'variant':22} {'prompt':8} {'n':>3} {'ok':>3} {'FP':>3} {'FN':>3} {'fail':>4} {'usd':>8} | baseline ok/FP/FN")
for r in rows:
    print(f"{r[0]:>3} {r[1]:11} {r[2][:48]:48} {r[3]:22} {r[5]:8} {r[6]:>3} {r[7]:>3} {r[8]:>3} {r[9]:>3} {r[10]:>4} {r[11]:>8.4f} | {r[12]}/{r[13]}/{r[14]}")

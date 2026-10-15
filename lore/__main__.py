"""Command line: fetch a project, load it into FalkorDB, ask it questions.

  python -m lore fetch FalkorDB                 # an org (its most active repos) or owner/repo
  python -m lore load data/FalkorDB.json
  python -m lore who-knows FalkorDB index
  python -m lore related FalkorDB "FalkorDB/FalkorDB#1234"
  python -m lore reviewers FalkorDB "FalkorDB/FalkorDB#1234"
  python -m lore risk FalkorDB
"""
import argparse
import gzip
import json
import os
import sys

from . import db, github, queries
from .config import Caps
from .load import load_snapshot

DATA = os.path.join(os.path.dirname(__file__), "..", "data")


def read_snapshot(path):
    with (gzip.open(path, "rt") if path.endswith(".gz") else open(path)) as fh:
        return json.load(fh)


def main(argv=None):
    ap = argparse.ArgumentParser(prog="lore")
    sub = ap.add_subparsers(dest="cmd", required=True)
    f = sub.add_parser("fetch")
    f.add_argument("target")
    f.add_argument("--repos", help="comma-separated owner/repo list instead of the org's most active")
    f.add_argument("--months", type=int, default=12)
    f.add_argument("--max-repos", type=int, default=10)
    f.add_argument("--max-prs", type=int, default=500)
    f.add_argument("--max-review-calls", type=int, default=150)
    f.add_argument("--out", help="snapshot path; .json.gz is gzipped (default data/<target>.json)")
    lo = sub.add_parser("load")
    lo.add_argument("snapshot")
    bt = sub.add_parser("backtest")
    bt.add_argument("graph")
    bt.add_argument("--days", type=int, default=365)
    bt.add_argument("--limit", type=int, default=300)
    bt.add_argument("--llm", type=int, default=0, help="also ask Gemini alone for this many PRs")
    for name in ("who-knows", "related", "reviewers", "risk"):
        q = sub.add_parser(name)
        q.add_argument("graph")
        if name != "risk":
            q.add_argument("arg")
        q.add_argument("--trace", action="store_true", help="also print the Cypher that ran")
    a = ap.parse_args(argv)

    if a.cmd == "fetch":
        caps = Caps(months=a.months, max_repos=a.max_repos, max_prs=a.max_prs, max_review_calls=a.max_review_calls)
        snap = github.fetch(a.target, DATA, token=os.environ.get("GITHUB_TOKEN"), caps=caps,
                            repos=a.repos.split(",") if a.repos else None)
        if not snap["repos"]:
            sys.exit("Nothing fetched: " + ", ".join(snap["incomplete"]))
        out = a.out or os.path.join(DATA, db.graph_name(a.target) + ".json")
        os.makedirs(os.path.dirname(os.path.abspath(out)), exist_ok=True)
        with (gzip.open(out, "wt") if out.endswith(".gz") else open(out, "w")) as fh:
            json.dump(snap, fh)
        print(f"Snapshot: {out}")
        return

    if a.cmd == "load":
        snap = read_snapshot(a.snapshot)
        g = db.connect().select_graph(db.graph_name(snap["target"]))
        counts = load_snapshot(g, snap)
        print(f"Graph {db.graph_name(snap['target'])}: " + ", ".join(f"{k} {v}" for k, v in counts.items()))
        return

    g = db.connect().select_graph(db.graph_name(a.graph))
    if a.cmd == "backtest":
        from . import backtest
        out = backtest.run(g, days=a.days, limit=a.limit, llm_sample=a.llm)
        path = os.path.join(DATA, "backtest", db.graph_name(a.graph) + ".json")
        os.makedirs(os.path.dirname(path), exist_ok=True)
        with open(path, "w") as fh:
            json.dump(out, fh, indent=1)
        print(f"{out['prs']} PRs. Share where the top pick / top 3 include a real reviewer:")
        for m, r in out["summary"].items():
            if r["n"]:
                print(f"  {m:9} top1 {r['top1']:.0%}  top3 {r['top3']:.0%}  (n={r['n']})")
        print(f"Saved {path}")
        return
    trace = queries.Trace()
    if a.cmd == "who-knows":
        out = queries.who_knows(g, a.arg, trace=trace)
    elif a.cmd == "related":
        out = queries.related(g, a.arg, trace=trace)
    elif a.cmd == "reviewers":
        out = queries.suggest_reviewers(g, a.arg, trace=trace)
    else:
        out = queries.knowledge_risk(g, trace=trace)
    json.dump({"answer": out, "trace": trace} if a.trace else out, sys.stdout, indent=2, default=str)
    print()


if __name__ == "__main__":
    main()

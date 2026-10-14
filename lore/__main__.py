"""Command line: fetch a project, load it into FalkorDB, ask it questions.

  python -m lore fetch FalkorDB                 # an org (its most active repos) or owner/repo
  python -m lore load data/FalkorDB.json
  python -m lore who-knows FalkorDB index
  python -m lore related FalkorDB "FalkorDB/FalkorDB#1234"
  python -m lore reviewers FalkorDB "FalkorDB/FalkorDB#1234"
  python -m lore risk FalkorDB
"""
import argparse
import json
import os
import sys

from . import db, github, queries
from .config import Caps
from .load import load_snapshot

DATA = os.path.join(os.path.dirname(__file__), "..", "data")


def main(argv=None):
    ap = argparse.ArgumentParser(prog="lore")
    sub = ap.add_subparsers(dest="cmd", required=True)
    f = sub.add_parser("fetch")
    f.add_argument("target")
    f.add_argument("--repos", help="comma-separated owner/repo list instead of the org's most active")
    f.add_argument("--months", type=int, default=12)
    f.add_argument("--max-repos", type=int, default=10)
    f.add_argument("--max-prs", type=int, default=500)
    lo = sub.add_parser("load")
    lo.add_argument("snapshot")
    for name in ("who-knows", "related", "reviewers", "risk"):
        q = sub.add_parser(name)
        q.add_argument("graph")
        if name != "risk":
            q.add_argument("arg")
        q.add_argument("--trace", action="store_true", help="also print the Cypher that ran")
    a = ap.parse_args(argv)

    if a.cmd == "fetch":
        caps = Caps(months=a.months, max_repos=a.max_repos, max_prs=a.max_prs)
        snap = github.fetch(a.target, DATA, token=os.environ.get("GITHUB_TOKEN"), caps=caps,
                            repos=a.repos.split(",") if a.repos else None)
        out = os.path.join(DATA, db.graph_name(a.target) + ".json")
        with open(out, "w") as fh:
            json.dump(snap, fh)
        print(f"Snapshot: {out}")
        return

    if a.cmd == "load":
        with open(a.snapshot) as fh:
            snap = json.load(fh)
        g = db.connect().select_graph(db.graph_name(snap["target"]))
        counts = load_snapshot(g, snap)
        print(f"Graph {db.graph_name(snap['target'])}: " + ", ".join(f"{k} {v}" for k, v in counts.items()))
        return

    g = db.connect().select_graph(db.graph_name(a.graph))
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

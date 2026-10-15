"""Backtest: for past PRs, would Lore have named the people who actually reviewed them?

For each merged PR that had reviewers, Lore scores everyone using only what happened before the PR was opened
(the same weights and decay as KNOWS, computed in the graph as of that date), and we check whether its top 1
and top 3 include a real reviewer. The PR's own reviews come later, so they can't leak in.

Baselines, given the same history:
  frequent  the repo's most frequent reviewers so far
  recent    the authors of the most recent merged PRs that touched the same files
  llm       Gemini alone, given the PR's title and the repo's contributors (optional, uses free quota)
"""
import json
import os
import re
import time

from .config import DECAY_K, W_AUTHOR, W_COMMENT, W_REVIEW
from .models import list_models
from . import agent

CANDIDATES = """
MATCH (p:PR)-[:TOUCHES]->(:File)
WHERE p.merged_at >= $since AND p.merged_at IS NOT NULL
WITH DISTINCT p
MATCH (author:Person)-[:AUTHORED]->(p)
MATCH (r:Person)-[:REVIEWED]->(p) WHERE r <> author
RETURN p.id AS id, p.repo AS repo, p.title AS title, p.created_at AS asof, author.login AS author,
       collect(DISTINCT r.login) AS reviewers
ORDER BY asof DESC LIMIT $limit"""

AREAS = "MATCH (:PR {id: $id})-[:TOUCHES]->(:File)-[:IN]->(a:Area) RETURN collect(DISTINCT a.id) AS areas"

AS_OF = {
    "authored": (W_AUTHOR, """
        MATCH (a:Area) WHERE a.id IN $areas
        MATCH (p:Person)-[:AUTHORED]->(pr:PR)-[:TOUCHES]->(:File)-[:IN]->(a)
        WHERE pr.merged_at < $asof
        WITH DISTINCT p, pr, a, pr.merged_at AS at"""),
    "reviewed": (W_REVIEW, """
        MATCH (a:Area) WHERE a.id IN $areas
        MATCH (p:Person)-[e:REVIEWED]->(pr:PR)-[:TOUCHES]->(:File)-[:IN]->(a)
        WHERE e.at < $asof
        WITH DISTINCT p, pr, a, e.at AS at"""),
    "commented": (W_COMMENT, """
        MATCH (a:Area) WHERE a.id IN $areas
        MATCH (p:Person)-[e:COMMENTED_ON]->(n:Item)<-[:FIXES*0..1]-(pr:PR)-[:TOUCHES]->(:File)-[:IN]->(a)
        WHERE e.last_at < $asof AND NOT (p)-[:AUTHORED]->(pr)
        WITH DISTINCT p, pr, a, e.last_at AS at"""),
}
SCORE = " RETURN p.login AS login, sum($w * exp($k * ($asof - at))) AS s"

FREQUENT = """
MATCH (r:Person)-[e:REVIEWED]->(pr:PR {repo: $repo})
WHERE e.at < $asof
RETURN r.login AS login, count(*) AS n ORDER BY n DESC LIMIT 6"""

RECENT = """
MATCH (:PR {id: $id})-[:TOUCHES]->(f:File)<-[:TOUCHES]-(pr:PR)<-[:AUTHORED]-(p:Person)
WHERE pr.merged_at < $asof
RETURN p.login AS login, max(pr.merged_at) AS last ORDER BY last DESC LIMIT 6"""

CONTRIBUTORS = """
MATCH (p:Person)-[:AUTHORED|REVIEWED]->(pr:PR {repo: $repo})
WHERE coalesce(pr.merged_at, pr.created_at) < $asof
RETURN p.login AS login, count(*) AS n ORDER BY n DESC LIMIT 25"""


def _rows(g, cypher, params):
    res = g.query(cypher, params)
    names = [h[1] for h in res.header]
    return [dict(zip(names, r)) for r in res.result_set]


def lore_pick(g, pr, k=3, weights=None, decay_k=DECAY_K):
    areas = _rows(g, AREAS, {"id": pr["id"]})[0]["areas"]
    scores = {}
    for kind, (weight, match) in AS_OF.items():
        weight = (weights or {}).get(kind, weight)
        for row in _rows(g, match + SCORE, {"areas": areas, "asof": pr["asof"], "w": weight, "k": decay_k}):
            scores[row["login"]] = scores.get(row["login"], 0) + row["s"]
    scores.pop(pr["author"], None)
    return sorted(scores, key=scores.get, reverse=True)[:k]


def frequent_pick(g, pr, k=3):
    return [r["login"] for r in _rows(g, FREQUENT, {"repo": pr["repo"], "asof": pr["asof"]}) if r["login"] != pr["author"]][:k]


def recent_pick(g, pr, k=3):
    return [r["login"] for r in _rows(g, RECENT, {"id": pr["id"], "asof": pr["asof"]}) if r["login"] != pr["author"]][:k]


def llm_pick(g, pr, model, k=3):
    people = [r["login"] for r in _rows(g, CONTRIBUTORS, {"repo": pr["repo"], "asof": pr["asof"]}) if r["login"] != pr["author"]]
    prompt = (f"A pull request was opened in {pr['repo']}: \"{pr['title']}\".\n"
              f"Contributors: {', '.join(people)}.\nWhich three contributors should review it? "
              "Reply with three logins separated by commas, most likely first, nothing else.")
    body = {"contents": [{"role": "user", "parts": [{"text": prompt}]}], "generationConfig": {"temperature": 0}}
    for m in [model] + [x for x in list_models() if x != model]:  # busy, out of quota or retired: next model
        data, err = agent._call(m, os.environ.get("GEMINI_API_KEY"), body)
        if not err:
            break
    if err:
        raise RuntimeError(err)
    text = "".join(p.get("text", "") for p in data["candidates"][0]["content"]["parts"] if not p.get("thought"))
    picks = [w.strip(" @.`") for w in re.split(r"[,\n]", text) if w.strip()]
    return [p for p in picks if p in people][:k]


def run(g, days=180, limit=200, llm_sample=0, now=None, log=print):
    now = now or int(time.time())
    prs = _rows(g, CANDIDATES, {"since": now - days * 86400, "limit": limit})
    methods = {"lore": lore_pick, "frequent": frequent_pick, "recent": recent_pick}
    hits = {m: {"top1": 0, "top3": 0, "n": 0} for m in list(methods) + (["llm"] if llm_sample else [])}
    examples = []
    model = list_models()[0] if llm_sample else None
    for i, pr in enumerate(prs):
        real = set(pr["reviewers"])
        row = {"pr": pr["id"], "title": pr["title"], "reviewers": sorted(real)}
        picks_by = {m: fn(g, pr) for m, fn in methods.items()}
        if llm_sample and i < llm_sample:
            for attempt in (1, 2):
                try:
                    time.sleep(7)  # free tier: stay under its requests-per-minute limit
                    picks_by["llm"] = llm_pick(g, pr, model)
                    break
                except RuntimeError as e:
                    if attempt == 2 or not str(e).startswith("429"):
                        log(f"  llm stopped at PR {i}: {e}")
                        llm_sample = i
                        break
                    time.sleep(30)
        for m, picks in picks_by.items():
            hits[m]["n"] += 1
            hits[m]["top1"] += bool(picks[:1] and picks[0] in real)
            hits[m]["top3"] += bool(real & set(picks[:3]))
            row[m] = picks
        examples.append(row)
    summary = {m: {"n": h["n"], "top1": round(h["top1"] / h["n"], 3) if h["n"] else None,
                   "top3": round(h["top3"] / h["n"], 3) if h["n"] else None} for m, h in hits.items()}
    return {"days": days, "prs": len(prs), "llm_model": model, "summary": summary, "examples": examples}

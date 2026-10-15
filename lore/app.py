"""Lore's web app: the landing page, the workspace, and an API that streams the agent's steps.

Run: uvicorn lore.app:app --port 8000
"""
import glob
import hashlib
import json
import os
import time

from fastapi import FastAPI, HTTPException
from fastapi.responses import FileResponse, StreamingResponse
from fastapi.staticfiles import StaticFiles
from pydantic import BaseModel

from . import agent, db, queries
from .__main__ import read_snapshot
from .load import LOADER_VERSION, load_snapshot
from .models import list_models

ROOT = os.path.abspath(os.path.join(os.path.dirname(__file__), ".."))
STATIC = os.path.join(ROOT, "web")
SNAPSHOTS = os.environ.get("LORE_SNAPSHOTS", os.path.join(ROOT, "data", "snapshots"))
SAVED = os.path.join(ROOT, "data", "answers")

app = FastAPI(title="Lore")
app.mount("/static", StaticFiles(directory=STATIC), name="static")
PROJECTS = {}  # graph name -> {"target", "fetched_at", "stats"}


STATS = "MATCH (n) UNWIND labels(n) AS kind RETURN kind, count(*) AS n"


def _load_snapshots():
    """Load each ready-made project's snapshot into its own graph, unless that graph already has it."""
    conn = db.connect()
    for path in sorted(glob.glob(os.path.join(SNAPSHOTS, "*.json*"))):
        snap = read_snapshot(path)
        name = db.graph_name(snap["target"])
        g = conn.select_graph(name)
        meta = []
        try:
            meta = g.query("MATCH (m:Meta) RETURN m.fetched_at, m.version").result_set
        except Exception:
            pass
        if not meta or meta[0] != [snap["fetched_at"], LOADER_VERSION]:
            if meta and meta[0][1] != LOADER_VERSION:
                g.delete()
                g = conn.select_graph(name)
            load_snapshot(g, snap)
            g.query("MERGE (m:Meta) SET m.fetched_at = $f, m.target = $t, m.version = $v",
                    {"f": snap["fetched_at"], "t": snap["target"], "v": LOADER_VERSION})
        stats = {row[0]: row[1] for row in g.query(STATS).result_set}
        PROJECTS[name] = {"name": name, "target": snap["target"], "fetched_at": snap["fetched_at"],
                          "repos": [r["name"] for r in snap["repos"]], "stats": stats}


@app.on_event("startup")
def startup():
    _load_snapshots()


def _graph(project):
    if project not in PROJECTS:
        raise HTTPException(404, f"No project {project}")
    return db.connect().select_graph(project)


# Pages -------------------------------------------------------------------------------------------

@app.get("/")
def landing():
    return FileResponse(os.path.join(STATIC, "index.html"))


@app.get("/app")
def workspace():
    return FileResponse(os.path.join(STATIC, "app.html"))


@app.get("/how")
def how():
    return FileResponse(os.path.join(STATIC, "how.html"))


@app.get("/EXPLAIN.md")
def explain():
    return FileResponse(os.path.join(ROOT, "EXPLAIN.md"), media_type="text/markdown")


# API ---------------------------------------------------------------------------------------------

@app.get("/api/projects")
def projects():
    return list(PROJECTS.values())


@app.get("/api/models")
def models():
    try:
        names = list_models()
    except Exception as e:
        return {"models": [], "default": None, "error": str(e)}
    return {"models": names, "default": names[0] if names else None}


OPEN_ISSUES = """
MATCH (i:Issue {state: 'open'})
OPTIONAL MATCH (i)-[:HAS_LABEL]->(l:Label)
OPTIONAL MATCH (:Person)-[c:COMMENTED_ON]->(i)
WITH i, collect(DISTINCT l.name) AS labels, sum(c.count) AS comments
RETURN i.id AS id, i.title AS title, i.url AS url, i.created_at AS created_at, labels, comments
ORDER BY created_at DESC LIMIT 60"""


@app.get("/api/issues")
def issues(project: str, filter: str = "newest"):
    rows = queries.Trace().run(_graph(project), "open_issues", OPEN_ISSUES)
    if filter == "unlabelled":
        rows = [r for r in rows if not r["labels"]]
    elif filter == "no-reply":
        rows = [r for r in rows if not r["comments"]]
    return rows[:30]


@app.get("/api/risk")
def risk(project: str):
    trace = queries.Trace()
    return {"areas": queries.knowledge_risk(_graph(project), trace=trace), "trace": trace}


class Ask(BaseModel):
    project: str
    question: str
    model: str | None = None
    key: str | None = None      # the visitor's own Gemini key: used for this request only, never stored
    suggested: bool = False     # a suggested question: its answer is saved, and replayed if every model fails


def _saved_path(project, question):
    return os.path.join(SAVED, project, hashlib.sha1(question.strip().lower().encode()).hexdigest()[:16] + ".json")


@app.post("/api/ask")
def ask(req: Ask):
    g = _graph(req.project)
    target = PROJECTS[req.project]["target"]

    def stream():
        events = []
        failed = False
        for e in agent.run(g, req.question, target, model=req.model, key=req.key or None):
            if e["type"] == "error" and req.suggested and os.path.exists(_saved_path(req.project, req.question)):
                failed = True
                break
            events.append(e)
            yield f"data: {json.dumps(e, default=str)}\n\n"
        if failed:
            yield f"data: {json.dumps({'type': 'replay', 'message': 'Every free model is busy, so this is a saved answer to the same question.'})}\n\n"
            with open(_saved_path(req.project, req.question)) as f:
                for e in json.load(f):
                    time.sleep(0.05)
                    yield f"data: {json.dumps(e, default=str)}\n\n"
            return
        if req.suggested and events and events[-1]["type"] == "answer":
            path = _saved_path(req.project, req.question)
            os.makedirs(os.path.dirname(path), exist_ok=True)
            with open(path, "w") as f:
                json.dump(events, f, default=str)

    return StreamingResponse(stream(), media_type="text/event-stream", headers={"Cache-Control": "no-cache"})


class Proof(BaseModel):
    project: str
    people: list[str] = []
    items: list[str] = []
    areas: list[str] = []


PROOF_WORK = """
MATCH (p:Person)-[r:AUTHORED|REVIEWED]->(pr:PR)-[:TOUCHES]->(:File)-[:IN]->(a:Area)
WHERE p.login IN $people AND (pr.id IN $items OR a.id IN $areas)
RETURN DISTINCT p.login AS person, type(r) AS rel, pr.id AS pr, pr.title AS title, pr.url AS url,
       a.id AS area, a.path AS path
LIMIT 300"""

PROOF_LINKS = """
MATCH (x:Item)-[r:FIXES|REFERS_TO]->(y:Item)
WHERE x.id IN $items AND y.id IN $items
RETURN x.id AS src, type(r) AS rel, y.id AS dst"""


@app.post("/api/proof")
def proof(req: Proof):
    """The evidence behind an answer, as a small graph: people, the PRs they wrote or reviewed, the areas."""
    g = _graph(req.project)
    trace = queries.Trace()
    params = {"people": req.people, "items": req.items, "areas": req.areas}
    nodes, edges = {}, []
    for row in trace.run(g, "proof_work", PROOF_WORK, params):
        nodes[row["person"]] = {"id": row["person"], "label": "@" + row["person"], "kind": "person"}
        nodes[row["pr"]] = {"id": row["pr"], "label": "#" + row["pr"].split("#")[1], "title": row["title"],
                            "url": row["url"], "kind": "pr"}
        nodes[row["area"]] = {"id": row["area"], "label": row["path"], "kind": "area"}
        edges.append({"source": row["person"], "target": row["pr"], "label": row["rel"].lower()})
        edges.append({"source": row["pr"], "target": row["area"], "label": "touches"})
    for row in trace.run(g, "proof_links", PROOF_LINKS, {"items": req.items}):
        for end in (row["src"], row["dst"]):
            nodes.setdefault(end, {"id": end, "label": "#" + end.split("#")[1], "kind": "item"})
        edges.append({"source": row["src"], "target": row["dst"], "label": row["rel"].lower()})
    unique = {(e["source"], e["target"], e["label"]): e for e in edges}
    return {"nodes": list(nodes.values()), "edges": list(unique.values()), "trace": trace}

"""Lore's web app: the landing page, the workspace, and an API that streams the agent's steps.

Run: uvicorn lore.app:app --port 8000
"""
import glob
import hashlib
import hmac
import json
import os
import re
import secrets
import threading
import time
from urllib.parse import urlencode

import requests

from fastapi import FastAPI, HTTPException, Request
from fastapi.responses import FileResponse, JSONResponse, RedirectResponse, StreamingResponse
from fastapi.staticfiles import StaticFiles
from pydantic import BaseModel

from . import agent, db, github, memory, queries
from .config import Caps
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
        stats = _stats(g)
        PROJECTS[name] = {"name": name, "target": snap["target"], "fetched_at": snap["fetched_at"],
                          "repos": [r["name"] for r in snap["repos"]], "stats": stats}


REGISTRY = "lore__registry"


def _stats(g):
    return {row[0]: row[1] for row in g.query(STATS).result_set}


def _load_registry():
    """User-added projects live in their own graphs; the registry lists them so they come back after a restart."""
    conn = db.connect()
    try:
        rows = conn.select_graph(REGISTRY).query("MATCH (p:Project) RETURN p.name, p.target, p.fetched_at, p.repos, p.added_by").result_set
    except Exception:
        return
    for name, target, fetched_at, repos, added_by in rows:
        if name not in PROJECTS:
            try:
                PROJECTS[name] = {"name": name, "target": target, "fetched_at": fetched_at, "repos": repos,
                                  "stats": _stats(conn.select_graph(name)), "added_by": added_by}
            except Exception:
                pass


@app.on_event("startup")
def startup():
    _load_snapshots()
    _load_registry()


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


@app.get("/README.md")
def readme():
    return FileResponse(os.path.join(ROOT, "README.md"), media_type="text/markdown")


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
def ask(req: Ask, request: Request):
    g = _graph(req.project)
    target = PROJECTS[req.project]["target"]
    user = _user(request)
    note = None
    memory_event = None
    if user:
        note, mem, changed = memory.context(user["login"], req.project, g)
        memory_event = {"type": "memory", "login": user["login"],
                        "interests": [i["path"] for i in mem["interests"]], "asked": len(mem["questions"]),
                        "changed": changed}

    def stream():
        events = []
        failed = False
        if memory_event:
            yield f"data: {json.dumps(memory_event, default=str)}\n\n"
        for e in agent.run(g, req.question, target, model=req.model, key=req.key or None, user_note=note):
            if e["type"] == "answer" and user:
                memory.remember(user["login"], req.project, req.question, e["text"], e["evidence"]["areas"])
                memory.mark_seen(user["login"], req.project)
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


# Only the PRs the answer cited, and the areas they touched. With no cited PRs (a knowledge-risk answer),
# each person's three most recent PRs in the named areas.
PROOF_WORK = """
MATCH (p:Person)-[r:AUTHORED|REVIEWED]->(pr:PR)-[:TOUCHES]->(:File)-[:IN]->(a:Area)
WHERE p.login IN $people AND pr.id IN $items AND (size($areas) = 0 OR a.id IN $areas)
RETURN DISTINCT p.login AS person, type(r) AS rel, pr.id AS pr, pr.title AS title, pr.url AS url,
       a.id AS area, a.path AS path
LIMIT 120"""

PROOF_AREAS = """
MATCH (p:Person)-[r:AUTHORED|REVIEWED]->(pr:PR)-[:TOUCHES]->(:File)-[:IN]->(a:Area)
WHERE p.login IN $people AND a.id IN $areas
WITH DISTINCT p, r, pr, a ORDER BY coalesce(pr.merged_at, pr.created_at) DESC
WITH p, a, collect({rel: type(r), pr: pr})[0..3] AS recent
UNWIND recent AS x
RETURN p.login AS person, x.rel AS rel, x.pr.id AS pr, x.pr.title AS title, x.pr.url AS url, a.id AS area, a.path AS path"""

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
    rows = trace.run(g, "proof_work", PROOF_WORK, params)
    if not rows and req.areas:
        rows = trace.run(g, "proof_areas", PROOF_AREAS, params)
    for row in rows:
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


# Teams --------------------------------------------------------------------------------------------

@app.get("/api/teams")
def teams(project: str):
    trace = queries.Trace()
    return {"teams": queries.teams(_graph(project), trace=trace), "trace": trace}


# Sign in with GitHub ------------------------------------------------------------------------------
# No scopes are asked for, so the token can only read public data. Sessions live in this process only.

SESSIONS = {}
_KEY = hashlib.sha256((os.environ.get("GITHUB_CLIENT_SECRET") or secrets.token_hex(32)).encode() + b"lore-session").digest()


def _sign(value):
    return value + "." + hmac.new(_KEY, value.encode(), hashlib.sha256).hexdigest()[:32]


def _unsign(signed):
    value, _, sig = (signed or "").rpartition(".")
    return value if value and hmac.compare_digest(_sign(value), signed) else None


def _user(request):
    sid = _unsign(request.cookies.get("lore_sid"))
    user = SESSIONS.get(sid) if sid else None
    return user if user and user["exp"] > time.time() else None


def _base(request):
    if os.environ.get("PUBLIC_URL"):
        return os.environ["PUBLIC_URL"].rstrip("/")
    return f"{request.headers.get('x-forwarded-proto', request.url.scheme)}://{request.headers.get('host')}"


@app.get("/auth/login")
def login(request: Request):
    if not os.environ.get("GITHUB_CLIENT_ID"):
        raise HTTPException(503, "Sign-in isn't set up on this server (GITHUB_CLIENT_ID).")
    state = secrets.token_urlsafe(16)
    url = "https://github.com/login/oauth/authorize?" + urlencode({
        "client_id": os.environ["GITHUB_CLIENT_ID"], "redirect_uri": _base(request) + "/auth/callback",
        "state": state, "allow_signup": "true"})
    resp = RedirectResponse(url)
    resp.set_cookie("lore_state", _sign(state), max_age=600, httponly=True, secure=True, samesite="lax")
    return resp


@app.get("/auth/callback")
def callback(request: Request, code: str = "", state: str = ""):
    if not state or _unsign(request.cookies.get("lore_state")) != state:
        raise HTTPException(400, "Sign-in expired or was tampered with. Try again.")
    tok = requests.post("https://github.com/login/oauth/access_token", headers={"Accept": "application/json"}, timeout=20,
                        data={"client_id": os.environ["GITHUB_CLIENT_ID"], "client_secret": os.environ["GITHUB_CLIENT_SECRET"],
                              "code": code, "redirect_uri": _base(request) + "/auth/callback"}).json()
    token = tok.get("access_token")
    if not token:
        raise HTTPException(400, "GitHub didn't sign you in: " + tok.get("error_description", "no token"))
    me = requests.get("https://api.github.com/user", headers={"Authorization": f"Bearer {token}"}, timeout=20).json()
    sid = secrets.token_urlsafe(24)
    SESSIONS[sid] = {"login": me["login"], "token": token, "exp": time.time() + 8 * 3600}
    resp = RedirectResponse("/app")
    resp.set_cookie("lore_sid", _sign(sid), max_age=8 * 3600, httponly=True, secure=True, samesite="lax")
    resp.delete_cookie("lore_state")
    return resp


@app.get("/auth/logout")
def logout(request: Request):
    sid = _unsign(request.cookies.get("lore_sid"))
    SESSIONS.pop(sid, None)
    resp = RedirectResponse("/")
    resp.delete_cookie("lore_sid")
    return resp


@app.get("/api/me")
def me(request: Request):
    user = _user(request)
    return {"login": user["login"] if user else None, "signin": bool(os.environ.get("GITHUB_CLIENT_ID")),
            "persistent": db.is_remote()}


# Memory -------------------------------------------------------------------------------------------

@app.get("/api/memory")
def my_memory(request: Request, project: str):
    user = _user(request)
    if not user:
        raise HTTPException(401, "Sign in to see what Lore remembers about you.")
    mem = memory.recall(user["login"], project)
    mem["changed"] = memory.changed_since(_graph(project), mem["interests"], mem["last_seen"])
    return mem


@app.delete("/api/memory")
def forget_me(request: Request):
    user = _user(request)
    if not user:
        raise HTTPException(401, "Sign in first.")
    memory.forget(user["login"])
    return {"forgotten": True}


# Add a project ------------------------------------------------------------------------------------

JOBS = {}
TARGET_RE = re.compile(r"^[A-Za-z0-9_.-]+(/[A-Za-z0-9_.-]+)?$")
USER_CAPS = Caps(months=12, max_repos=3, max_prs=300, max_issues=300, max_comment_pages=5, max_review_calls=60)


class AddProject(BaseModel):
    target: str


def _build(name, target, token, login):
    job = JOBS[name]
    try:
        snap = github.fetch(target, os.path.join(ROOT, "data", "user"), token=token, caps=USER_CAPS,
                            log=lambda m: job["log"].append(m.strip()))
        if not snap["repos"]:
            raise RuntimeError("nothing could be fetched: " + ", ".join(snap["incomplete"]))
        g = db.connect().select_graph(name)
        job["log"].append("Building the graph and computing who knows what…")
        counts = load_snapshot(g, snap)
        g.query("MERGE (m:Meta) SET m.fetched_at = $f, m.target = $t, m.version = $v",
                {"f": snap["fetched_at"], "t": target, "v": LOADER_VERSION})
        repos = [r["name"] for r in snap["repos"]]
        db.connect().select_graph(REGISTRY).query(
            "MERGE (p:Project {name: $n}) SET p.target = $t, p.fetched_at = $f, p.repos = $r, p.added_by = $by",
            {"n": name, "t": target, "f": snap["fetched_at"], "r": repos, "by": login})
        PROJECTS[name] = {"name": name, "target": target, "fetched_at": snap["fetched_at"], "repos": repos,
                          "stats": _stats(g), "added_by": login}
        job["log"].append("Done: " + ", ".join(f"{v} {k}" for k, v in counts.items()))
        job["status"] = "done"
    except Exception as e:
        job["log"].append(f"Failed: {e}")
        job["status"] = "failed"


@app.post("/api/projects/add")
def add_project(req: AddProject, request: Request):
    user = _user(request)
    if not user:
        raise HTTPException(401, "Sign in with GitHub to add a project: building one uses your GitHub allowance.")
    target = req.target.strip().removeprefix("https://github.com/").strip("/")
    if not TARGET_RE.match(target):
        raise HTTPException(400, "Use an org name (FalkorDB) or owner/repo (FalkorDB/falkordb-py).")
    name = db.graph_name(target)
    if name in PROJECTS:
        return {"name": name, "status": "done", "log": ["Already here."]}
    if any(j["status"] == "running" for j in JOBS.values()):
        raise HTTPException(429, "Another project is being built right now. Try again in a few minutes.")
    JOBS[name] = {"name": name, "status": "running", "log": [f"Fetching {target} with @{user['login']}'s GitHub allowance…"]}
    threading.Thread(target=_build, args=(name, target, user["token"], user["login"]), daemon=True).start()
    return JOBS[name]


@app.get("/api/projects/progress")
def progress(name: str):
    if name not in JOBS:
        raise HTTPException(404, "No such build")
    return JOBS[name]

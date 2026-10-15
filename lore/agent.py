"""The agent: Gemini picks graph queries as tools, reads their results, and answers with sources.

run() is a generator of events, so the app can show each step as it happens ("Under the hood"):
  {"type": "model", "model"}                   which model is answering
  {"type": "call", "tool", "args"}             the tool Gemini chose
  {"type": "query", "step", "cypher", "rows", "ms"}   each Cypher query that tool ran
  {"type": "result", "tool", "summary", "data"}       what came back
  {"type": "fallback", "from", "reason"}       a model failed (usually its free limit); the next one takes over
  {"type": "answer", "text", "model", "evidence"}
  {"type": "error", "message"}
"""
import json
import os

import requests

from . import queries
from .models import API, fallback_order, list_models

SYSTEM = """You are Lore. You know the open-source project "{project}" through a graph of its public GitHub history:
people (GitHub logins), pull requests, reviews, issues, comments, files, and areas of the code (directories).

Rules:
- Answer only from tool results. Never invent people, PRs, issues, dates or reasons.
- Cite a source for every person, claim or decision, as [owner/repo#123]. Write people as @login.
- Who knows something: call find_areas to turn the topic into area ids, then who_knows with those ids.
- Why something is the way it is: call recall_decisions first. If nothing is remembered, search_history, read the
  best one or two sources with get_item, explain the reason with citations, then call save_decision with a
  one-sentence summary and the ids of the sources you used.
- About an issue: get_item, then related and suggest_reviewers. Never post anything anywhere: you only draft.
- First contribution: find_areas for the person's interest, then good_first_issues with those area ids, then who_knows.
- If the graph does not hold the answer, say so plainly and say what you looked for.
- Be brief: a few sentences or a short list."""


def _obj(props, required=()):
    return {"type": "OBJECT", "properties": props, "required": list(required)}


S = {"type": "STRING"}
TOOLS = [
    {"name": "find_areas", "description": "Areas of the code (directories) whose path or files mention the words. Use it to turn a topic into area ids.",
     "parameters": _obj({"text": S}, ["text"])},
    {"name": "who_knows", "description": "People ranked by how well they know the given areas (recent merged PRs, reviews, comments), with the PRs that prove it.",
     "parameters": _obj({"area_ids": {"type": "ARRAY", "items": S}, "active_only": {"type": "BOOLEAN"}}, ["area_ids"])},
    {"name": "get_item", "description": "One issue or PR: title, description, state, author, labels. Accepts 'owner/repo#12' or '#12'.",
     "parameters": _obj({"ref": S}, ["ref"])},
    {"name": "search_history", "description": "Full-text search over PR and issue titles and descriptions: where something was discussed or decided.",
     "parameters": _obj({"text": S}, ["text"])},
    {"name": "related", "description": "Past issues and PRs related to an issue, each with the reason, plus the areas the issue is about.",
     "parameters": _obj({"issue_ref": S}, ["issue_ref"])},
    {"name": "suggest_reviewers", "description": "Active people who know an issue's areas best (never its author), with evidence.",
     "parameters": _obj({"issue_ref": S}, ["issue_ref"])},
    {"name": "knowledge_risk", "description": "Areas with recent work that at most one active person knows well.",
     "parameters": _obj({})},
    {"name": "good_first_issues", "description": "Open beginner-friendly issues, closest to the given areas first.",
     "parameters": _obj({"area_ids": {"type": "ARRAY", "items": S}})},
    {"name": "recall_decisions", "description": "Decisions Lore has already worked out and saved, with their sources.",
     "parameters": _obj({"text": S}, ["text"])},
    {"name": "save_decision", "description": "Remember a decision you explained: a one-sentence summary and the PR/issue ids it came from.",
     "parameters": _obj({"summary": S, "source_ids": {"type": "ARRAY", "items": S}}, ["summary", "source_ids"])},
]


def execute(g, name, args, trace):
    """Run one tool against the graph."""
    if name == "find_areas":
        return queries.find_areas(g, args.get("text", ""), trace=trace)
    if name == "who_knows":
        return queries.who_knows(g, areas=args.get("area_ids") or [], active_only=bool(args.get("active_only")), trace=trace)
    if name == "get_item":
        return queries.get_item(g, args.get("ref", ""), trace=trace) or {"error": "no such issue or PR in this project's graph"}
    if name == "search_history":
        return queries.search_history(g, args.get("text", ""), trace=trace)
    if name in ("related", "suggest_reviewers"):
        item = queries.resolve_item(g, args.get("issue_ref", ""), trace=trace)
        if not item:
            return {"error": "no such issue in this project's graph"}
        fn = queries.related if name == "related" else queries.suggest_reviewers
        return fn(g, item, trace=trace)
    if name == "knowledge_risk":
        return queries.knowledge_risk(g, trace=trace)
    if name == "good_first_issues":
        return queries.good_first_issues(g, args.get("area_ids") or [], trace=trace)
    if name == "recall_decisions":
        return queries.recall_decisions(g, args.get("text", ""), trace=trace)
    if name == "save_decision":
        return queries.save_decision(g, args.get("summary", ""), args.get("source_ids") or [], trace=trace)
    return {"error": f"unknown tool {name}"}


def _summary(result):
    if isinstance(result, list):
        return f"{len(result)} result(s)"
    if isinstance(result, dict) and "error" in result:
        return result["error"]
    if isinstance(result, dict) and "related" in result:
        return f"{len(result['related'])} related, areas: {', '.join(a['path'] for a in result['areas']) or 'none found'}"
    if isinstance(result, dict) and "reviewers" in result:
        return f"{len(result['reviewers'])} reviewer(s)"
    return "done"


def _evidence(result, out):
    """Collect every PR/issue and person a tool returned, for the Proof panel."""
    if isinstance(result, dict):
        if result.get("url") and result.get("id"):
            out["items"].setdefault(result["id"], {"id": result["id"], "title": result.get("title"), "url": result["url"]})
        if result.get("pr") and result.get("url"):
            out["items"].setdefault(result["pr"], {"id": result["pr"], "title": result.get("title"), "url": result["url"]})
        if result.get("login"):
            out["people"].add(result["login"])
        if result.get("area") and result.get("path"):
            out["areas"].setdefault(result["area"], result["path"])
        for v in result.values():
            _evidence(v, out)
    elif isinstance(result, list):
        for v in result:
            _evidence(v, out)


def _call(model, key, body):
    try:
        r = requests.post(f"{API}/models/{model}:generateContent", json=body,
                          headers={"x-goog-api-key": key}, timeout=90)
    except requests.RequestException as e:
        return None, str(e)
    if r.status_code != 200:
        try:
            msg = r.json().get("error", {}).get("message", "")
        except ValueError:
            msg = r.text[:200]
        return None, f"{r.status_code}: {msg[:160]}"
    return r.json(), None


def run(g, question, project, model=None, key=None, max_steps=8):
    key = key or os.environ.get("GEMINI_API_KEY")
    if not key:
        yield {"type": "error", "message": "No Gemini key: set GEMINI_API_KEY or paste your own key."}
        return
    try:
        order = fallback_order(model, list_models(key))
    except requests.RequestException as e:
        yield {"type": "error", "message": f"Couldn't list Gemini models: {e}"}
        return
    body = {
        "systemInstruction": {"parts": [{"text": SYSTEM.format(project=project)}]},
        "contents": [{"role": "user", "parts": [{"text": question}]}],
        "tools": [{"functionDeclarations": TOOLS}],
        "generationConfig": {"temperature": 0.2},
    }
    evidence = {"items": {}, "people": set(), "areas": {}}
    i = 0
    yield {"type": "model", "model": order[0]}
    for _ in range(max_steps):
        while True:
            data, err = _call(order[i], key, body)
            content = (data or {}).get("candidates", [{}])[0].get("content") if data else None
            if content and content.get("parts"):
                break
            yield {"type": "fallback", "from": order[i], "reason": err or "empty response"}
            i += 1
            if i >= len(order):
                yield {"type": "error", "message": "Every available Gemini model failed or is out of free quota. Try again later or use your own key."}
                return
            yield {"type": "model", "model": order[i]}
        body["contents"].append(content)  # as returned, so thought signatures go back to the model
        calls = [p["functionCall"] for p in content["parts"] if "functionCall" in p]
        if not calls:
            text = "".join(p.get("text", "") for p in content["parts"] if not p.get("thought"))
            yield {"type": "answer", "text": text.strip(), "model": order[i],
                   "evidence": {"items": list(evidence["items"].values()), "people": sorted(evidence["people"]),
                                "areas": [{"id": k, "path": v} for k, v in evidence["areas"].items()]}}
            return
        responses = []
        for c in calls:
            args = c.get("args") or {}
            yield {"type": "call", "tool": c["name"], "args": args}
            trace = queries.Trace()
            try:
                result = execute(g, c["name"], args, trace)
            except Exception as e:  # a bad query must not kill the answer; the model sees the error
                result = {"error": f"{type(e).__name__}: {e}"}
            for q in trace:
                yield {"type": "query", **q}
            _evidence(result, evidence)
            yield {"type": "result", "tool": c["name"], "summary": _summary(result), "data": result}
            payload = json.loads(json.dumps(result, default=str))
            resp = {"name": c["name"], "response": {"result": payload}}
            if c.get("id"):
                resp["id"] = c["id"]
            responses.append({"functionResponse": resp})
        body["contents"].append({"role": "user", "parts": responses})
    yield {"type": "error", "message": "Stopped after too many steps without an answer."}

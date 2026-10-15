"""The questions Lore answers from the graph. Each records the Cypher it ran in a trace,
which is what the "Under the hood" panel shows, and returns evidence for every claim."""
import re
import time


STOPWORDS = set("""a an and are as at be but by can cannot could did do does doesn't don't each for from get got
had has have how i if in into is isn't it its it's just like make may more most my no not of on or our out
over same should so some such than that the their them then there these they this to too up use used using
very was we were what when where which while who why will with would you your after before between both
error issue problem bug fails failed failing crash when work works working add support new""".split())


class Trace(list):
    """Every query Lore runs while answering: name, Cypher, row count and time."""

    def run(self, g, name, cypher, params=None):
        t0 = time.time()
        res = g.query(cypher, params or {})
        self.append({"step": name, "cypher": " ".join(cypher.split()), "rows": len(res.result_set),
                     "ms": round((time.time() - t0) * 1000, 1)})
        names = [h[1] for h in res.header]
        return [dict(zip(names, row)) for row in res.result_set]


def tokens(text):
    words = re.findall(r"[a-z][a-z0-9_]{2,}", (text or "").lower())
    return {w for w in words if w not in STOPWORDS}


# 1. Who knows this area? ------------------------------------------------------------------------

WHO_KNOWS = """
MATCH (a:Area) WHERE a.id IN $areas OR ($term <> '' AND toLower(a.path) CONTAINS toLower($term))
MATCH (p:Person)-[k:KNOWS]->(a)
WITH p, sum(k.score) AS score, collect(a.path) AS areas, max(k.last_at) AS last_at
RETURN p.login AS login, score, areas, last_at, p.active AS active
ORDER BY score DESC LIMIT $limit"""

EVIDENCE = """
MATCH (p:Person {login: $login})-[r:AUTHORED|REVIEWED]->(pr:PR)-[:TOUCHES]->(:File)-[:IN]->(a:Area)
WHERE a.id IN $areas OR ($term <> '' AND toLower(a.path) CONTAINS toLower($term))
WITH DISTINCT type(r) AS role, pr, collect(DISTINCT a.path) AS areas
RETURN role, pr.id AS pr, pr.title AS title, pr.url AS url, coalesce(pr.merged_at, pr.created_at) AS at, areas
ORDER BY at DESC LIMIT $limit"""


def who_knows(g, term="", areas=None, limit=5, active_only=False, trace=None):
    """People ranked by KNOWS across the matching areas, each with the PRs behind it."""
    trace = trace if trace is not None else Trace()
    params = {"term": term or "", "areas": areas or [], "limit": limit * 3 if active_only else limit}
    people = trace.run(g, "who_knows", WHO_KNOWS, params)
    if active_only:
        people = [p for p in people if p["active"]][:limit]
    for p in people:
        p["score"] = round(p["score"], 2)
        p["evidence"] = trace.run(g, f"evidence:{p['login']}", EVIDENCE,
                                  {"login": p["login"], "term": params["term"], "areas": params["areas"], "limit": 3})
    return people


# Which areas does an issue touch? ------------------------------------------------------------------

AREAS_FROM_FIXES = """
MATCH (i:Issue {id: $id})<-[:FIXES]-(pr:PR)-[:TOUCHES]->(:File)-[:IN]->(a:Area)
RETURN a.id AS area, a.path AS path, count(DISTINCT pr) AS n"""

AREAS_FROM_LABELS = """
MATCH (i:Issue {id: $id})-[:HAS_LABEL]->(l:Label)<-[:HAS_LABEL]-(o:Issue)<-[:FIXES]-(pr:PR)-[:TOUCHES]->(:File)-[:IN]->(a:Area)
WHERE o <> i
WITH l, a, count(DISTINCT pr) AS n
MATCH (l)<-[:HAS_LABEL]-(:Issue)<-[:FIXES]-(all:PR)
WITH l, a, n, count(DISTINCT all) AS total
RETURN a.id AS area, a.path AS path, l.name AS label, toFloat(n) / total AS share"""

AREA_NAMES = """
MATCH (a:Area {repo: $repo})<-[:IN]-(f:File)
RETURN a.id AS area, a.path AS path, collect(f.path) AS files"""


def issue_areas(g, issue_id, top=3, trace=None):
    """Score the areas an issue is about, with the reason for each:
    PRs that fixed it (strongest), labels whose past issues were fixed in that area,
    and words in its title and body that name the area or a file in it."""
    trace = trace if trace is not None else Trace()
    issue = trace.run(g, "issue", "MATCH (i:Issue {id: $id}) RETURN i.repo AS repo, i.title AS title, i.body AS body",
                      {"id": issue_id})
    if not issue:
        return []
    issue = issue[0]
    scores, why = {}, {}

    def add(area, path, s, reason):
        scores[area] = scores.get(area, 0) + s
        why.setdefault(area, {"path": path, "reasons": []})["reasons"].append(reason)

    for row in trace.run(g, "areas_from_fixes", AREAS_FROM_FIXES, {"id": issue_id}):
        add(row["area"], row["path"], 3.0 * row["n"], f"fixed by {row['n']} PR(s) touching it")
    for row in trace.run(g, "areas_from_labels", AREAS_FROM_LABELS, {"id": issue_id}):
        if row["share"] >= 0.2:
            add(row["area"], row["path"], 2.0 * row["share"],
                f"{round(100 * row['share'])}% of past '{row['label']}' fixes touched it")
    words = tokens(f"{issue['title']} {issue['body']}")
    for row in trace.run(g, "area_names", AREA_NAMES, {"repo": issue["repo"]}):
        segs = {s.lower() for s in row["path"].split("/")} - {"src", "lib", "(root)"}
        stems = {re.sub(r"\.\w+$", "", f.rsplit("/", 1)[-1]).lower() for f in row["files"]}
        stem_words = {w for s in stems for w in re.split(r"[_\-.]", s) if len(w) > 3} - STOPWORDS
        hit_segs, hit_files = words & segs, words & stem_words
        if hit_segs or hit_files:
            add(row["area"], row["path"], 1.0 * len(hit_segs) + 0.5 * min(len(hit_files), 3),
                "mentions " + ", ".join(sorted(hit_segs | hit_files)))
    ranked = sorted(scores, key=scores.get, reverse=True)[:top]
    return [{"area": a, "path": why[a]["path"], "score": round(scores[a], 2), "reasons": why[a]["reasons"]}
            for a in ranked]


# 3. Related issues and PRs ---------------------------------------------------------------------

DIRECT_LINKS = """
MATCH (i:Issue {id: $id})-[:REFERS_TO|FIXES*1..2]-(o:Item)
WHERE o <> i
RETURN DISTINCT o.id AS id, o.title AS title, o.url AS url, o.state AS state, [l IN labels(o) WHERE l <> 'Item'][0] AS kind"""

SAME_AREA = """
MATCH (a:Area) WHERE a.id IN $areas
MATCH (a)<-[:IN]-(:File)<-[:TOUCHES]-(pr:PR)-[:FIXES]->(o:Issue)
WHERE o.id <> $id
RETURN o.id AS id, o.title AS title, o.url AS url, o.state AS state,
       count(DISTINCT a) AS shared, collect(DISTINCT pr.id) AS via"""

TITLES = """MATCH (o:Issue {repo: $repo}) WHERE o.id <> $id RETURN o.id AS id, o.title AS title, o.url AS url, o.state AS state"""


def related(g, issue_id, limit=5, trace=None):
    """Past issues and PRs linked to this one: direct references (two hops), issues fixed in the
    same areas, and similar titles. Each result says why it's related."""
    trace = trace if trace is not None else Trace()
    issue = trace.run(g, "issue", "MATCH (i:Issue {id: $id}) RETURN i.repo AS repo, i.title AS title", {"id": issue_id})
    if not issue:
        return {"areas": [], "related": []}
    areas = issue_areas(g, issue_id, trace=trace)
    found = {}

    def add(row, s, reason, kind="Issue"):
        cur = found.setdefault(row["id"], {"id": row["id"], "title": row["title"], "url": row["url"],
                                           "state": row["state"], "kind": row.get("kind", kind), "score": 0, "why": []})
        cur["score"] += s
        cur["why"].append(reason)

    for row in trace.run(g, "direct_links", DIRECT_LINKS, {"id": issue_id}):
        add(row, 3.0, "linked directly", row["kind"])
    for row in trace.run(g, "same_area", SAME_AREA, {"id": issue_id, "areas": [a["area"] for a in areas]}):
        add(row, 1.5 * row["shared"], f"fixed in the same area by {', '.join(row['via'][:2])}")
    mine = tokens(issue[0]["title"])
    if mine:
        for row in trace.run(g, "similar_titles", TITLES, {"id": issue_id, "repo": issue[0]["repo"]}):
            theirs = tokens(row["title"])
            overlap = len(mine & theirs) / len(mine | theirs) if theirs else 0
            if overlap >= 0.25:
                add(row, 2.0 * overlap, "similar title: " + ", ".join(sorted(mine & theirs)))
    ranked = sorted(found.values(), key=lambda r: r["score"], reverse=True)[:limit]
    for r in ranked:
        r["score"] = round(r["score"], 2)
    return {"areas": areas, "related": ranked}


# 4. Who should look at this issue? ---------------------------------------------------------------

REVIEWERS = """
MATCH (a:Area) WHERE a.id IN $areas
MATCH (p:Person)-[k:KNOWS]->(a)
WHERE p.active AND NOT (p)-[:OPENED]->(:Issue {id: $id})
WITH p, sum(k.score) AS score, collect(a.path) AS areas
RETURN p.login AS login, score, areas ORDER BY score DESC LIMIT $limit"""


def suggest_reviewers(g, issue_id, limit=3, trace=None):
    """Active people who know the issue's areas best, never its author, each with evidence."""
    trace = trace if trace is not None else Trace()
    areas = issue_areas(g, issue_id, trace=trace)
    area_ids = [a["area"] for a in areas]
    people = trace.run(g, "reviewers", REVIEWERS, {"areas": area_ids, "id": issue_id, "limit": limit})
    for p in people:
        p["score"] = round(p["score"], 2)
        p["evidence"] = trace.run(g, f"evidence:{p['login']}", EVIDENCE,
                                  {"login": p["login"], "term": "", "areas": area_ids, "limit": 3})
    return {"areas": areas, "reviewers": people}


# 6. Knowledge risk -------------------------------------------------------------------------------

RISK = """
MATCH (a:Area)<-[:IN]-(:File)<-[:TOUCHES]-(pr:PR)
WHERE pr.merged_at >= $since
WITH a, count(DISTINCT pr) AS recent_prs
WHERE recent_prs >= $min_prs
OPTIONAL MATCH (p:Person)-[k:KNOWS]->(a)
WHERE p.active AND k.score >= $min_score
WITH a, recent_prs, collect(p.login) AS knowers
WHERE size(knowers) <= 1
RETURN a.id AS area, a.path AS path, a.repo AS repo, recent_prs, knowers
ORDER BY recent_prs DESC"""


NOT_KNOWLEDGE = {"images", "img", "assets", "static", "_includes", ".github", "public", "fonts"}


def knowledge_risk(g, days=180, min_prs=3, min_score=1.0, now=None, trace=None):
    """Areas with recent work that at most one active person knows well."""
    trace = trace if trace is not None else Trace()
    now = now or int(time.time())
    rows = trace.run(g, "knowledge_risk", RISK, {"since": now - days * 86400, "min_prs": min_prs, "min_score": min_score})
    # Assets and CI config aren't knowledge in the sense that matters here.
    return [r for r in rows if not set(r["path"].split("/")) & NOT_KNOWLEDGE]



# Tools the agent uses to find its way around ---------------------------------------------------------

FIND_AREAS = """
MATCH (a:Area)<-[:IN]-(f:File)
WHERE any(w IN $words WHERE toLower(a.path) CONTAINS w OR toLower(f.path) CONTAINS w)
WITH a, count(DISTINCT f) AS files
OPTIONAL MATCH (a)<-[:IN]-(:File)<-[:TOUCHES]-(pr:PR)
RETURN a.id AS area, a.path AS path, a.repo AS repo, files, count(DISTINCT pr) AS prs
ORDER BY prs DESC LIMIT 8"""


def find_areas(g, text, trace=None):
    """Areas whose directory or files mention any of the words, busiest first."""
    trace = trace if trace is not None else Trace()
    words = sorted(tokens(text) | {w for w in re.findall(r"[a-z0-9_]{3,}", (text or "").lower())})
    return trace.run(g, "find_areas", FIND_AREAS, {"words": words}) if words else []


GET_ITEM = """
MATCH (i:Item {id: $id})
OPTIONAL MATCH (i)-[:HAS_LABEL]->(l:Label)
OPTIONAL MATCH (p:Person)-[:OPENED|AUTHORED]->(i)
RETURN i.id AS id, [l IN labels(i) WHERE l <> 'Item'][0] AS kind, i.title AS title, substring(coalesce(i.body, ''), 0, 1200) AS body,
       i.state AS state, i.url AS url, p.login AS author, collect(l.name) AS labels"""

RESOLVE = "MATCH (i:Item {number: $n}) RETURN i.id AS id ORDER BY i.created_at DESC LIMIT 1"


def resolve_item(g, ref, trace=None):
    """'owner/repo#12', '#12' or '12' -> an Item id in this graph, or None."""
    trace = trace if trace is not None else Trace()
    ref = str(ref).strip()
    if "/" in ref and "#" in ref:
        return ref
    m = re.search(r"(\d+)", ref)
    if not m:
        return None
    rows = trace.run(g, "resolve_item", RESOLVE, {"n": int(m.group(1))})
    return rows[0]["id"] if rows else None


def get_item(g, ref, trace=None):
    trace = trace if trace is not None else Trace()
    item_id = resolve_item(g, ref, trace)
    rows = trace.run(g, "get_item", GET_ITEM, {"id": item_id}) if item_id else []
    return rows[0] if rows else None


SEARCH_HISTORY = """
CALL db.idx.fulltext.queryNodes('Item', $q) YIELD node, score
OPTIONAL MATCH (p:Person)-[:OPENED|AUTHORED]->(node)
RETURN node.id AS id, [l IN labels(node) WHERE l <> 'Item'][0] AS kind, node.title AS title, node.state AS state, node.url AS url,
       substring(coalesce(node.body, ''), 0, 600) AS excerpt, p.login AS author,
       coalesce(node.merged_at, node.created_at) AS at, score
ORDER BY score DESC LIMIT $limit"""


def search_history(g, text, limit=8, trace=None):
    """Full-text search over PR and issue titles and descriptions: where things were discussed and decided."""
    trace = trace if trace is not None else Trace()
    words = [w for w in re.findall(r"[a-z0-9]{3,}", (text or "").lower()) if w not in STOPWORDS]
    if not words:
        return []
    return trace.run(g, "search_history", SEARCH_HISTORY, {"q": " | ".join(words[:8]), "limit": limit})


GOOD_FIRST = """
MATCH (i:Issue {state: 'open'})-[:HAS_LABEL]->(l:Label)
WHERE any(t IN ['good first issue', 'good-first-issue', 'help wanted', 'beginner', 'easy', 'first-timers-only']
          WHERE toLower(l.name) CONTAINS t)
RETURN DISTINCT i.id AS id, i.title AS title, i.url AS url, i.created_at AS created_at, collect(l.name) AS labels
ORDER BY created_at DESC LIMIT 25"""


def good_first_issues(g, area_ids=None, limit=5, trace=None):
    """Open beginner-labelled issues; with areas given, those closest to them come first."""
    trace = trace if trace is not None else Trace()
    issues = trace.run(g, "good_first_issues", GOOD_FIRST)
    wanted = set(area_ids or [])
    for i in issues:
        areas = issue_areas(g, i["id"], trace=trace) if wanted else []
        i["areas"] = [a["path"] for a in areas]
        i["match"] = len(wanted & {a["area"] for a in areas})
    issues.sort(key=lambda i: i["match"], reverse=True)
    return issues[:limit]


# Memory: what Lore has learned, kept in the graph with its sources ------------------------------------

SAVE_DECISION = """
MATCH (s:Item) WHERE s.id IN $sources
WITH collect(s) AS sources WHERE size(sources) > 0
CREATE (d:Decision {id: $id, summary: $summary, created_at: $now})
FOREACH (s IN sources | MERGE (d)-[:SOURCE]->(s))
WITH d, sources UNWIND sources AS s
OPTIONAL MATCH (s)-[:TOUCHES]->(:File)-[:IN]->(a:Area)
WITH d, collect(DISTINCT a) AS areas
FOREACH (a IN areas | MERGE (d)-[:ABOUT]->(a))
RETURN d.id AS id, size(areas) AS areas"""

RECALL_DECISIONS = """
MATCH (d:Decision)-[:SOURCE]->(s:Item)
WHERE any(w IN $words WHERE toLower(d.summary) CONTAINS w)
RETURN d.id AS id, d.summary AS summary, d.created_at AS at, collect(s.id) AS sources, collect(s.url) AS urls
ORDER BY at DESC LIMIT 5"""


def save_decision(g, summary, source_ids, trace=None):
    """Store an explained decision as a node linked to the PRs/issues it came from. Refused without sources."""
    trace = trace if trace is not None else Trace()
    if not source_ids:
        return {"saved": False, "reason": "a decision needs at least one source"}
    rows = trace.run(g, "save_decision", SAVE_DECISION, {
        "id": f"decision-{int(time.time() * 1000)}", "summary": summary[:600],
        "sources": list(source_ids), "now": int(time.time())})
    return {"saved": bool(rows), **(rows[0] if rows else {})}


def recall_decisions(g, text, trace=None):
    trace = trace if trace is not None else Trace()
    words = [w for w in tokens(text)][:8]
    return trace.run(g, "recall_decisions", RECALL_DECISIONS, {"words": words}) if words else []


# 7 and 8. Teams nobody drew, and who holds them together ------------------------------------------------

TEAMS = """
CALL algo.labelPropagation({nodeLabels: ['Person'], relationshipTypes: ['WORKS_CLOSELY']}) YIELD node, communityId
WITH communityId, collect(node.login) AS members
WHERE size(members) > 1
RETURN communityId AS team, members ORDER BY size(members) DESC"""

CENTRAL = """
CALL algo.pageRank('Person', 'WORKS_WITH') YIELD node, score
RETURN node.login AS login, score, node.active AS active"""

TEAM_AREAS = """
MATCH (p:Person)-[k:KNOWS]->(a:Area) WHERE p.login IN $members
RETURN a.path AS path, a.repo AS repo, sum(k.score) AS score ORDER BY score DESC LIMIT 4"""


def teams(g, trace=None):
    """Community detection over strong review ties (TEAM_MIN_REVIEWS or more between two people): the teams that
    actually work together, each with the people at its centre (PageRank over all review ties) and its best-known areas."""
    trace = trace if trace is not None else Trace()
    rank = {r["login"]: r for r in trace.run(g, "pagerank", CENTRAL)}
    out = []
    for t in trace.run(g, "label_propagation", TEAMS):
        members = sorted(t["members"], key=lambda m: rank.get(m, {}).get("score", 0), reverse=True)
        out.append({"team": t["team"], "size": len(members),
                    "members": [{"login": m, "centrality": round(rank.get(m, {}).get("score", 0), 3),
                                 "active": bool(rank.get(m, {}).get("active"))} for m in members],
                    "areas": trace.run(g, f"team_areas:{t['team']}", TEAM_AREAS, {"members": members})})
    return out

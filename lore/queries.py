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
RETURN DISTINCT o.id AS id, o.title AS title, o.url AS url, o.state AS state, labels(o)[0] AS kind"""

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


def knowledge_risk(g, days=180, min_prs=3, min_score=1.0, now=None, trace=None):
    """Areas with recent work that at most one active person knows well."""
    trace = trace if trace is not None else Trace()
    now = now or int(time.time())
    return trace.run(g, "knowledge_risk", RISK,
                     {"since": now - days * 86400, "min_prs": min_prs, "min_score": min_score})


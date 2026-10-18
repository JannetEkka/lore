"""What Lore remembers about each signed-in person: one graph per user (FalkorDB's multi-graph), so a
person's memory is isolated, survives project rebuilds, and deleting it is deleting one graph.

(u:User)-[:ASKED]->(q:Question {project, text, at, answer})-[:ABOUT]->(t:Topic {project, area, path})
(u)-[:INTERESTED_IN {weight, last_at}]->(t)
(u)-[:SEEN {at}]->(p:Project {name})
"""
import time

from . import db


def graph(login):
    return db.connect().select_graph("user__" + login.lower())


def _rows(g, cypher, params=None):
    res = g.query(cypher, params or {})
    names = [h[1] for h in res.header]
    return [dict(zip(names, r)) for r in res.result_set]


def remember(login, project, question, answer, areas, now=None):
    """Store a question, its answer and the areas it was about; strengthen the person's interest in them."""
    now = now or int(time.time())
    g = graph(login)
    g.query("""MERGE (u:User {login: $login})
               CREATE (u)-[:ASKED]->(q:Question {project: $project, text: $text, answer: $answer, at: $now})
               WITH u, q UNWIND $areas AS a
               MERGE (t:Topic {project: $project, area: a.id}) ON CREATE SET t.path = a.path
               MERGE (q)-[:ABOUT]->(t)
               MERGE (u)-[i:INTERESTED_IN]->(t)
               ON CREATE SET i.weight = 1, i.last_at = $now
               ON MATCH SET i.weight = i.weight + 1, i.last_at = $now""",
            {"login": login, "project": project, "text": question[:500], "answer": (answer or "")[:1500],
             "areas": areas or [], "now": now})


def recall(login, project):
    """Recent questions, strongest interests and the last visit to this project."""
    g = graph(login)
    try:
        questions = _rows(g, """MATCH (:User {login: $login})-[:ASKED]->(q:Question {project: $project})
                                RETURN q.text AS text, q.at AS at ORDER BY q.at DESC LIMIT 8""", {"login": login, "project": project})
        interests = _rows(g, """MATCH (:User {login: $login})-[i:INTERESTED_IN]->(t:Topic {project: $project})
                                RETURN t.area AS area, t.path AS path, i.weight AS weight, i.last_at AS last_at
                                ORDER BY i.weight DESC, i.last_at DESC LIMIT 6""", {"login": login, "project": project})
        seen = _rows(g, """MATCH (:User {login: $login})-[s:SEEN]->(:Project {name: $project})
                           RETURN CASE WHEN s.at < timestamp() / 1000 - 7200 THEN s.at ELSE s.prev END AS at""",
                     {"login": login, "project": project})
    except Exception:  # no memory graph yet
        return {"questions": [], "interests": [], "last_seen": None}
    return {"questions": questions, "interests": interests, "last_seen": seen[0]["at"] if seen else None}


def mark_seen(login, project, now=None):
    """Record activity. `prev` keeps when the previous visit happened (a gap of 2 hours starts a new visit),
    so "since your last visit" doesn't reset with every question."""
    graph(login).query("""MERGE (u:User {login: $login}) MERGE (p:Project {name: $project})
                          MERGE (u)-[s:SEEN]->(p)
                          SET s.prev = CASE WHEN s.at IS NULL THEN $now
                                            WHEN s.at < $now - 7200 THEN s.at ELSE coalesce(s.prev, s.at) END,
                              s.at = $now""",
                       {"login": login, "project": project, "now": now or int(time.time())})


CHANGED = """
MATCH (a:Area) WHERE a.id IN $areas
MATCH (a)<-[:IN]-(:File)<-[:TOUCHES]-(pr:PR)
WHERE pr.merged_at > $since
RETURN DISTINCT pr.id AS id, pr.title AS title, pr.url AS url, a.path AS area, pr.merged_at AS at
ORDER BY at DESC LIMIT 6"""


def changed_since(project_graph, interests, since):
    """Merged PRs in the person's areas since their last visit."""
    if not interests or not since:
        return []
    return _rows(project_graph, CHANGED, {"areas": [i["area"] for i in interests], "since": since})


def context(login, project, project_graph):
    """A short note for the agent: who it's talking to and what it remembers."""
    mem = recall(login, project)
    if not mem["questions"] and not mem["interests"]:
        return f"You are talking to @{login}, signed in for the first time in this project.", mem, []
    changed = changed_since(project_graph, mem["interests"], mem["last_seen"])
    lines = [f"You are talking to @{login}. From their earlier sessions in this project:"]
    if mem["interests"]:
        lines.append("- interested in: " + ", ".join(f"{i['path']} ({i['area']})" for i in mem["interests"]))
    if mem["questions"]:
        lines.append("- asked before: " + " | ".join(q["text"] for q in mem["questions"][:4]))
    if changed:
        lines.append("- merged in their areas since their last visit: " + "; ".join(f"[{c['id']}] {c['title']}" for c in changed))
    lines.append("Use this when it helps (for example, say what changed since last time), and never invent more.")
    return "\n".join(lines), mem, changed


def forget(login):
    try:
        graph(login).delete()
    except Exception:
        pass

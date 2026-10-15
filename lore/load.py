"""Turn a snapshot into a FalkorDB graph, then compute who knows which area inside the graph."""
import time
from collections import defaultdict

from .areas import area_for
from .config import ACTIVE_DAYS, DECAY_K, W_AUTHOR, W_COMMENT, W_REVIEW

INDEXES = [
    ("Person", "login"), ("Repo", "name"), ("Area", "id"), ("File", "id"),
    ("Item", "id"), ("PR", "id"), ("Issue", "id"), ("Label", "id"),
]

REVIEW_RANK = {"APPROVED": 3, "CHANGES_REQUESTED": 2, "COMMENTED": 1, "DISMISSED": 0}


def _batches(rows, size=500):
    for i in range(0, len(rows), size):
        yield rows[i:i + size]


def _write(g, cypher, rows):
    for batch in _batches(rows):
        g.query(cypher, {"rows": batch})


def create_indexes(g):
    for label, prop in INDEXES:
        try:
            g.query(f"CREATE INDEX FOR (n:{label}) ON (n.{prop})")
        except Exception:
            pass  # already there


def rows_from_snapshot(snap):
    """Flatten the snapshot into the rows each Cypher write below takes."""
    r = defaultdict(list)
    activity = defaultdict(int)  # login -> latest activity, epoch seconds

    def seen(login, at):
        if login and at:
            activity[login] = max(activity[login], at)

    for repo in snap["repos"]:
        name = repo["name"]
        r["repos"].append({"name": name, "url": repo["url"]})
        pr_numbers = {p["number"] for p in repo["prs"]}
        issue_numbers = {i["number"] for i in repo["issues"]}

        def item_id(n):
            return f"{name}#{n}" if n in pr_numbers or n in issue_numbers else None

        for p in repo["prs"]:
            pid = f"{name}#{p['number']}"
            seen(p["author"], p["merged_at"] or p["created_at"])
            r["prs"].append({"id": pid, "repo": name, "number": p["number"], "title": p["title"], "body": p.get("body", ""),
                             "author": p["author"], "state": p["state"], "url": p["url"],
                             "created_at": p["created_at"], "merged_at": p["merged_at"]})
            for path in p.get("files", []):
                area = area_for(path)
                r["touches"].append({"pr": pid, "repo": name, "file": path, "file_id": f"{name}:{path}",
                                     "area": area, "area_id": f"{name}:{area}"})
            for n in p["fixes"]:
                if n in issue_numbers:
                    r["fixes"].append({"pr": pid, "issue": f"{name}#{n}"})
            for n in p["refs"]:
                if item_id(n) and n not in p["fixes"]:
                    r["refs"].append({"src": pid, "dst": item_id(n)})
            for label in p["labels"]:
                r["labels"].append({"item": pid, "id": f"{name}:{label}", "name": label, "repo": name})

        for i in repo["issues"]:
            iid = f"{name}#{i['number']}"
            seen(i["author"], i["created_at"])
            r["issues"].append({"id": iid, "repo": name, "number": i["number"], "title": i["title"],
                                "body": i["body"], "author": i["author"], "state": i["state"], "url": i["url"],
                                "created_at": i["created_at"], "closed_at": i["closed_at"]})
            for n in i["refs"]:
                if item_id(n):
                    r["refs"].append({"src": iid, "dst": item_id(n)})
            for label in i["labels"]:
                r["labels"].append({"item": iid, "id": f"{name}:{label}", "name": label, "repo": name})

        # One REVIEWED edge per person and PR: the strongest state and the latest time.
        best = {}
        for rv in repo["reviews"]:
            if rv["pr"] not in pr_numbers:
                continue
            seen(rv["author"], rv["at"])
            key = (rv["author"], rv["pr"])
            cur = best.get(key)
            if cur is None:
                best[key] = {"author": rv["author"], "pr": f"{name}#{rv['pr']}", "state": rv["state"], "at": rv["at"], "count": 1}
            else:
                cur["count"] += 1
                cur["at"] = max(cur["at"], rv["at"])
                if REVIEW_RANK.get(rv["state"], 0) > REVIEW_RANK.get(cur["state"], 0):
                    cur["state"] = rv["state"]
        r["reviews"].extend(best.values())

        # One COMMENTED_ON edge per person and item, with a count and the latest time.
        agg = {}
        for c in repo["comments"]:
            target = item_id(c["number"])
            if not target:
                continue
            seen(c["author"], c["at"])
            key = (c["author"], target)
            cur = agg.setdefault(key, {"author": c["author"], "item": target, "count": 0, "last_at": 0})
            cur["count"] += 1
            cur["last_at"] = max(cur["last_at"], c["at"])
        r["comments"].extend(agg.values())

    r["people"] = [{"login": login, "last_active": at} for login, at in activity.items()]
    return r


def load_snapshot(g, snap, now=None):
    """Write the snapshot into graph g and compute KNOWS. Safe to re-run: everything MERGEs."""
    r = rows_from_snapshot(snap)
    create_indexes(g)
    _write(g, "UNWIND $rows AS x MERGE (r:Repo {name: x.name}) SET r.url = x.url", r["repos"])
    _write(g, "UNWIND $rows AS x MERGE (p:Person {login: x.login}) SET p.last_active = x.last_active", r["people"])
    _write(g, """UNWIND $rows AS x
        MERGE (pr:PR:Item {id: x.id})
        SET pr.repo = x.repo, pr.number = x.number, pr.title = x.title, pr.body = x.body, pr.state = x.state, pr.url = x.url,
            pr.created_at = x.created_at, pr.merged_at = x.merged_at
        WITH pr, x MATCH (p:Person {login: x.author}) MERGE (p)-[:AUTHORED]->(pr)""", r["prs"])
    _write(g, """UNWIND $rows AS x
        MERGE (i:Issue:Item {id: x.id})
        SET i.repo = x.repo, i.number = x.number, i.title = x.title, i.body = x.body, i.state = x.state,
            i.url = x.url, i.created_at = x.created_at, i.closed_at = x.closed_at
        WITH i, x MATCH (p:Person {login: x.author}) MERGE (p)-[:OPENED]->(i)""", r["issues"])
    _write(g, """UNWIND $rows AS x
        MATCH (pr:PR {id: x.pr})
        MERGE (a:Area {id: x.area_id}) ON CREATE SET a.path = x.area, a.repo = x.repo
        MERGE (f:File {id: x.file_id}) ON CREATE SET f.path = x.file, f.repo = x.repo
        MERGE (f)-[:IN]->(a)
        MERGE (pr)-[:TOUCHES]->(f)""", r["touches"])
    g.query("MATCH (a:Area) MATCH (r:Repo {name: a.repo}) MERGE (a)-[:PART_OF]->(r)")
    _write(g, "UNWIND $rows AS x MATCH (pr:PR {id: x.pr}), (i:Issue {id: x.issue}) MERGE (pr)-[:FIXES]->(i)", r["fixes"])
    _write(g, "UNWIND $rows AS x MATCH (a:Item {id: x.src}), (b:Item {id: x.dst}) MERGE (a)-[:REFERS_TO]->(b)", r["refs"])
    _write(g, """UNWIND $rows AS x MATCH (n:Item {id: x.item})
        MERGE (l:Label {id: x.id}) ON CREATE SET l.name = x.name, l.repo = x.repo
        MERGE (n)-[:HAS_LABEL]->(l)""", r["labels"])
    _write(g, """UNWIND $rows AS x MATCH (p:Person {login: x.author}), (pr:PR {id: x.pr})
        MERGE (p)-[e:REVIEWED]->(pr) SET e.state = x.state, e.at = x.at, e.count = x.count""", r["reviews"])
    _write(g, """UNWIND $rows AS x MATCH (p:Person {login: x.author}), (n:Item {id: x.item})
        MERGE (p)-[e:COMMENTED_ON]->(n) SET e.count = x.count, e.last_at = x.last_at""", r["comments"])
    compute_knows(g, now)
    return {k: len(v) for k, v in r.items()}


# Each kind of work adds to KNOWS(person, area). A PR counts once per area however many files it touched,
# and older work counts less: weight * exp(DECAY_K * age) halves every HALF_LIFE_DAYS.
KNOWS_FROM = {
    "authored": (W_AUTHOR, """
        MATCH (p:Person)-[:AUTHORED]->(pr:PR)-[:TOUCHES]->(:File)-[:IN]->(a:Area)
        WHERE pr.merged_at IS NOT NULL
        WITH DISTINCT p, pr, a, pr.merged_at AS at"""),
    "reviewed": (W_REVIEW, """
        MATCH (p:Person)-[e:REVIEWED]->(pr:PR)-[:TOUCHES]->(:File)-[:IN]->(a:Area)
        WITH DISTINCT p, pr, a, e.at AS at"""),
    "commented": (W_COMMENT, """
        MATCH (p:Person)-[e:COMMENTED_ON]->(n:Item)
        MATCH (n)<-[:FIXES*0..1]-(pr:PR)-[:TOUCHES]->(:File)-[:IN]->(a:Area)
        WHERE NOT (p)-[:AUTHORED]->(pr)
        WITH DISTINCT p, pr, a, e.last_at AS at"""),
}


def compute_knows(g, now=None):
    now = now or int(time.time())
    g.query("MATCH (:Person)-[k:KNOWS]->(:Area) DELETE k")
    for kind, (weight, match) in KNOWS_FROM.items():
        g.query(match + f"""
            WITH p, a, sum($w * exp($k * ($now - at))) AS s, count(pr) AS n, max(at) AS last
            MERGE (p)-[k:KNOWS]->(a)
            ON CREATE SET k.score = s, k.{kind} = n, k.last_at = last
            ON MATCH SET k.score = k.score + s, k.{kind} = n,
                         k.last_at = CASE WHEN last > k.last_at THEN last ELSE k.last_at END""",
                {"w": weight, "k": DECAY_K, "now": now})
    g.query("MATCH (p:Person) SET p.active = p.last_active >= $cutoff", {"cutoff": now - ACTIVE_DAYS * 86400})

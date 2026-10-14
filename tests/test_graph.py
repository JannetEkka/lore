import math

import pytest

from lore import queries
from tests.conftest import NOW


def decay(days):
    return 0.5 ** (days / 180)


def knows(g, login, area_path):
    res = g.query("MATCH (:Person {login: $l})-[k:KNOWS]->(a:Area {path: $p}) RETURN k.score",
                  {"l": login, "p": area_path}).result_set
    return res[0][0] if res else 0.0


def test_knows_adds_authoring_reviews_and_comments_with_decay(graph):
    assert knows(graph, "alice", "src/index") == pytest.approx(decay(10) + decay(200), rel=1e-3)
    # bob: reviewed PR 10 (counted once, at the latest review), and commented on issue 1 that PR 10 fixed
    assert knows(graph, "bob", "src/index") == pytest.approx(0.6 * decay(9) + 0.2 * decay(12), rel=1e-3)
    assert knows(graph, "dave", "src/index") == pytest.approx(decay(300), rel=1e-3)
    assert knows(graph, "alice", "src/parser") == 0.0  # an open PR with no merged files counts for nothing


def test_active_flag(graph):
    rows = dict(graph.query("MATCH (p:Person) RETURN p.login, p.active").result_set)
    assert rows["alice"] and rows["bob"] and not rows["dave"]


def test_who_knows_ranks_with_evidence(graph):
    trace = queries.Trace()
    people = queries.who_knows(graph, "index", trace=trace)
    assert [p["login"] for p in people] == ["alice", "bob", "dave"]
    assert people[0]["evidence"][0]["pr"] == "acme/db#10"
    assert trace[0]["step"] == "who_knows" and "KNOWS" in trace[0]["cypher"]
    assert [p["login"] for p in queries.who_knows(graph, "index", active_only=True)] == ["alice", "bob"]


def test_issue_areas_from_labels_and_words(graph):
    areas = queries.issue_areas(graph, "acme/db#3")
    assert areas[0]["path"] == "src/index"
    reasons = " ".join(areas[0]["reasons"])
    assert "'index' fixes" in reasons and "mentions" in reasons


def test_issue_areas_from_fixing_pr(graph):
    areas = queries.issue_areas(graph, "acme/db#2")
    assert areas[0]["path"] == "src/parser"
    assert "fixed by 1 PR" in areas[0]["reasons"][0]


def test_related_finds_the_referenced_issue_first(graph):
    out = queries.related(graph, "acme/db#3")
    ids = [r["id"] for r in out["related"]]
    assert ids[0] == "acme/db#1"
    assert "acme/db#10" in ids  # the PR that fixed #1, two hops away
    assert "linked directly" in out["related"][0]["why"]


def test_reviewers_skip_the_author_and_inactive_people(graph):
    out = queries.suggest_reviewers(graph, "acme/db#3")
    assert [p["login"] for p in out["reviewers"]] == ["alice", "bob"]
    assert out["reviewers"][0]["evidence"]


def test_knowledge_risk_flags_areas_one_person_knows(graph):
    risky = queries.knowledge_risk(graph, min_prs=1, min_score=0.5, now=NOW)
    by_path = {r["path"]: r["knowers"] for r in risky}
    assert by_path.get("docs") == ["carol"]
    assert "src/index" not in by_path and "src/parser" not in by_path

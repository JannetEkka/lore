from lore import agent, queries


def test_find_areas_turns_a_topic_into_area_ids(graph):
    rows = queries.find_areas(graph, "vector index")
    assert rows[0]["area"] == "acme/db:src/index"


def test_search_history_finds_where_a_decision_was_explained(graph):
    rows = queries.search_history(graph, "why HNSW")
    assert rows[0]["id"] == "acme/db#10" and "brute force" in rows[0]["excerpt"]


def test_decisions_are_saved_with_sources_and_recalled(graph):
    assert not queries.save_decision(graph, "No sources", [])["saved"]
    saved = queries.save_decision(graph, "The vector index uses HNSW because brute force was too slow.", ["acme/db#10"])
    assert saved["saved"] and saved["areas"] == 1
    recalled = queries.recall_decisions(graph, "why hnsw")
    assert recalled[0]["sources"] == ["acme/db#10"]


def test_good_first_issues_prefer_the_asked_area(graph):
    rows = queries.good_first_issues(graph, ["acme/db:src/parser"])
    assert rows[0]["id"] == "acme/db#4" and rows[0]["match"] == 1


def test_tools_run_without_a_model(graph):
    trace = queries.Trace()
    out = agent.execute(graph, "suggest_reviewers", {"issue_ref": "#3"}, trace)
    assert [p["login"] for p in out["reviewers"]] == ["alice", "bob"]
    evidence = {"items": {}, "people": set(), "areas": {}}
    agent._evidence(out, evidence)
    assert evidence["people"] == {"alice", "bob"} and "acme/db#10" in evidence["items"]
    assert agent.execute(graph, "get_item", {"ref": "#999"}, trace) == {"error": "no such issue or PR in this project's graph"}

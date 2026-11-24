"""Partial data: what Lore measures about its own coverage, and what it does when the data is partial
(the fix for the review of Lore for Anna, Oct 28, 2026)."""
import copy

from lore import agent, app, coverage, github
from lore.config import Caps
from lore.report import summarise, to_markdown
from tests.conftest import SNAPSHOT


def snap(**changes):
    s = copy.deepcopy(SNAPSHOT)
    s.update(changes)
    return s


def test_complete_snapshot_is_not_partial():
    c = coverage.measure(SNAPSHOT)
    assert not c["partial"] and c["summary"] == "" and c["details"] == []
    assert c["merged_prs"] == 6 and c["files_for"] == 6 and c["approvals_for"] == 6


def test_each_limit_makes_it_partial_and_says_so():
    s = snap()
    s["repos"][0]["prs"][1]["files"] = []           # a merged PR git couldn't tie to files
    c = coverage.measure(s)
    assert c["partial"] and c["summary"] == "Files are known for 5 of 6 merged PRs"
    assert c["details"] == ["acme/db: files for 5 of 6 merged PRs"]

    s = snap()
    s["repos"][0]["limits"] = {"pr_cap_hit": True, "issue_cap_hit": False, "comment_cap_hit": False,
                               "review_comment_cap_hit": False, "approvals_for": 2}
    c = coverage.measure(s)
    assert c["summary"].startswith("Review approvals were read for 2 of them")
    assert "acme/db goes back only 500 PRs, not the full 12 months" in c["summary"]
    assert "PRs back to" in c["details"][0]

    c = coverage.measure(snap(has_approvals=False))
    assert c["partial"] and c["summary"].startswith("No review approvals were read")

    c = coverage.measure(snap(incomplete=["acme/other"]))
    assert c["partial"] and c["summary"].startswith("GitHub's limit stopped acme/other")


def test_old_snapshots_infer_the_default_caps():
    s = snap()
    s["repos"][0]["prs"] = s["repos"][0]["prs"] * 80  # 560 PRs, over the default 500
    assert coverage.measure(s)["repos"][0]["pr_cap_hit"]


def test_data_note_only_when_partial():
    assert coverage.data_note(coverage.measure(SNAPSHOT)) == ""
    note = coverage.data_note(coverage.measure(snap(has_approvals=False)))
    assert note.startswith("The data is PARTIAL") and "Based on the fetched data" in note


def test_absolute_claims_flags_unhedged_absolutes_and_lets_hedged_ones_through():
    partial = coverage.measure(snap(has_approvals=False))
    flagged = [
        "Only @eps1lon knows it well.",
        "**@eps1lon** is the only active maintainer of packages/react-dom.",
        "Nobody else has touched the scheduler.",
        "This area has a bus factor of 1.",
        "The compiler is a single point of failure.",
        "The docs depend entirely on @rickhanlonii.",
    ]
    for s in flagged:
        assert len(coverage.absolute_claims(partial, s)) == 1, s
    fine = [
        "Based on the fetched data, only @eps1lon knows it well.",
        "@eps1lon knows it best [facebook/react#1], and @acdlite reviewed #2.",
        "In the partial data Lore read, nobody else shows up for this area.",
        "The full history may show more people; only @a appears here.",
    ]
    for s in fine:
        assert coverage.absolute_claims(partial, s) == [], s
    assert coverage.absolute_claims(coverage.measure(SNAPSHOT), "Only @a knows it.") == []


def test_agent_tells_the_model_and_flags_an_absolute_answer(graph, monkeypatch):
    sent = []
    replies = iter([
        {"candidates": [{"content": {"role": "model", "parts": [{"functionCall": {"name": "who_knows", "args": {"area_ids": []}}}]}}]},
        {"candidates": [{"content": {"role": "model", "parts": [{"text": "Only @alice knows the index."}]}}]},
    ])
    monkeypatch.setattr(agent, "_order", lambda model, key: ["m"])
    monkeypatch.setattr(agent, "_call", lambda model, key, body: (sent.append(copy.deepcopy(body)) or next(replies), None))
    cov = coverage.measure(snap(has_approvals=False))
    events = list(agent.run(graph, "Who knows the index?", "acme/db", key="k", cov=cov))
    answer = events[-1]
    assert answer["type"] == "answer" and answer["partial"]
    assert answer["caveats"] == ["Only @alice knows the index."]
    assert "The data is PARTIAL" in sent[0]["systemInstruction"]["parts"][0]["text"]
    response = sent[1]["contents"][-1]["parts"][0]["functionResponse"]["response"]
    assert response["data_note"].startswith("Partial data:")


def test_agent_with_complete_data_adds_nothing(graph, monkeypatch):
    sent = []
    monkeypatch.setattr(agent, "_order", lambda model, key: ["m"])
    monkeypatch.setattr(agent, "_call", lambda model, key, body: (sent.append(body) or
                        {"candidates": [{"content": {"parts": [{"text": "Only @alice knows it."}]}}]}, None))
    answer = list(agent.run(graph, "q", "acme/db", key="k", cov=coverage.measure(SNAPSHOT)))[-1]
    assert answer["caveats"] == [] and not answer["partial"]
    assert "PARTIAL" not in sent[0]["systemInstruction"]["parts"][0]["text"]


def test_projects_added_before_coverage_count_as_partial():
    assert app._coverage_from(None)["partial"]
    assert app._coverage_from("not json")["partial"]
    assert app._coverage_from('{"partial": false, "summary": "", "details": []}') == {"partial": False, "summary": "", "details": []}


def test_report_says_when_it_rests_on_partial_data():
    s = snap()
    s["repos"][0]["prs"][1]["files"] = []
    md = to_markdown(summarise(s, months=12), 12)
    assert "**Based on partial data:** files known for 5 of 6 merged PRs" in md
    assert "Based on partial data" not in to_markdown(summarise(SNAPSHOT, months=12), 12)


class FakeGitHub:
    has_token = False

    def __init__(self, prs):
        self.prs = prs

    def pages(self, path, params, max_pages=50):
        if path.endswith("/pulls"):
            yield from self.prs
        return

    def get(self, path, params=None):
        return []


def test_fetch_records_when_the_pr_cap_cuts_the_list(monkeypatch, tmp_path):
    from datetime import datetime, timedelta, timezone
    now = datetime.now(timezone.utc)
    prs = [{"number": n, "title": f"PR {n}", "body": "", "user": {"login": "alice", "type": "User"},
            "created_at": (now - timedelta(days=n)).strftime("%Y-%m-%dT%H:%M:%SZ"), "merged_at": None,
            "state": "open", "labels": [], "html_url": f"https://github.com/o/r/pull/{n}"} for n in range(1, 6)]
    monkeypatch.setattr(github, "git_pr_files", lambda full, since, git_dir: {})
    since = now - timedelta(days=365)
    capped = github.fetch_repo(FakeGitHub(prs), "o/r", since, Caps(max_prs=3), str(tmp_path), log=lambda m: None)
    assert len(capped["prs"]) == 3 and capped["limits"]["pr_cap_hit"]
    whole = github.fetch_repo(FakeGitHub(prs), "o/r", since, Caps(max_prs=10), str(tmp_path), log=lambda m: None)
    assert not whole["limits"]["pr_cap_hit"] and whole["limits"]["approvals_for"] == 0

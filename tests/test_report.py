from lore.report import summarise, to_markdown

DAY = 86400


def _snap():
    now = 1_800_000_000
    prs = []
    for n in range(1, 7):   # six PRs in src/parser, five by alice
        prs.append({"number": n, "author": "alice" if n < 6 else "bob", "merged_at": now - n * DAY,
                    "state": "closed", "files": ["src/parser/p.py"]})
    prs.append({"number": 7, "author": "carol", "merged_at": now - 400 * DAY, "state": "closed",
                "files": ["src/old/x.py"]})                       # outside the window
    prs.append({"number": 8, "author": "dependabot[bot]", "merged_at": now - DAY, "state": "closed",
                "files": ["src/parser/p.py"]})                    # bots don't count
    return {"fetched_at": now, "has_approvals": True, "incomplete": [],
            "repos": [{"name": "o/r", "prs": prs, "issues": [{"state": "open"}], "reviews": [
                {"pr": 1, "author": "bob", "state": "APPROVED", "at": now}]}]}


def test_summary_counts_window_and_skips_bots():
    r = summarise(_snap(), months=6)[0]
    assert r["merged_prs"] == 6 and r["authors"] == 2
    assert r["one_author_areas"] == [{"area": "src/parser", "prs": 6, "person": "alice", "wrote": 5}]
    assert r["single_person_areas"] == []    # bob reviewed #1 and wrote #6


def test_markdown_names_the_area():
    md = to_markdown(summarise(_snap(), months=6), 6)
    assert "`src/parser`: @alice wrote 5 of 6" in md

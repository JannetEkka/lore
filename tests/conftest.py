import os
import sys
import uuid

import pytest

sys.path.insert(0, os.path.join(os.path.dirname(__file__), ".."))

from lore import db  # noqa: E402
from lore.load import load_snapshot  # noqa: E402

DAY = 86400
NOW = 1_800_000_000


def ago(days):
    return NOW - days * DAY


def pr(n, author, files, merged_days=None, created_days=None, fixes=(), refs=(), labels=()):
    return {"number": n, "title": f"PR {n}", "author": author,
            "state": "merged" if merged_days is not None else "open",
            "created_at": ago(created_days if created_days is not None else (merged_days or 0) + 1),
            "merged_at": ago(merged_days) if merged_days is not None else None,
            "labels": list(labels), "fixes": list(fixes), "refs": list(refs),
            "url": f"https://github.com/acme/db/pull/{n}", "files": list(files)}


def issue(n, author, title, created_days, labels=(), refs=(), body="", state="open"):
    return {"number": n, "title": title, "body": body, "author": author, "state": state,
            "created_at": ago(created_days), "closed_at": None, "labels": list(labels), "refs": list(refs),
            "url": f"https://github.com/acme/db/issues/{n}"}


SNAPSHOT = {
    "target": "acme/db", "since": ago(365), "fetched_at": NOW, "has_approvals": True, "incomplete": [],
    "repos": [{
        "name": "acme/db", "url": "https://github.com/acme/db",
        "prs": [
            pr(10, "alice", ["src/index/index.c", "src/index/vector.c"], merged_days=10, fixes=[1]),
            pr(11, "alice", ["src/index/index.c"], merged_days=200),
            pr(12, "bob", ["src/parser/parse.c"], merged_days=20, fixes=[2]),
            pr(13, "carol", ["src/parser/lexer.c"], merged_days=5),
            pr(14, "dave", ["src/index/btree.c"], merged_days=300),
            pr(15, "alice", [], created_days=2),
            pr(16, "carol", ["docs/guide.md"], merged_days=3),
        ],
        "issues": [
            issue(1, "erin", "Vector index crash on large dims", 15, labels=["bug", "index"], state="closed"),
            issue(2, "frank", "Parser fails on nested lists", 25, labels=["bug", "parser"], state="closed"),
            issue(3, "erin", "Crash when querying vector index after restart", 1, labels=["index"], refs=[1],
                  body="Same as before, the index breaks."),
            issue(4, "gina", "Lexer error with unicode", 1, labels=["parser"]),
        ],
        "reviews": [
            {"pr": 10, "author": "bob", "state": "APPROVED", "at": ago(9)},
            {"pr": 10, "author": "bob", "state": "COMMENTED", "at": ago(9.5)},
            {"pr": 12, "author": "carol", "state": "APPROVED", "at": ago(19)},
        ],
        "comments": [
            {"number": 1, "author": "bob", "at": ago(12)},
            {"number": 3, "author": "erin", "at": ago(1)},
        ],
    }],
}


@pytest.fixture(scope="session")
def database(tmp_path_factory):
    return db.connect(str(tmp_path_factory.mktemp("db") / "test.rdb"))


@pytest.fixture
def graph(database):
    g = database.select_graph("t_" + uuid.uuid4().hex[:8])
    load_snapshot(g, SNAPSHOT, now=NOW)
    yield g
    g.delete()

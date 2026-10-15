from lore.areas import ROOT, area_for, is_ignored
from lore.github import parse_git_log, parse_refs


def test_area_is_first_two_directories():
    assert area_for("src/index/vector/hnsw.c") == "src/index"
    assert area_for("src/main.c") == "src"
    assert area_for("README.md") == ROOT


def test_vendored_and_lock_files_are_ignored():
    assert is_ignored("deps/GraphBLAS/x.c")
    assert is_ignored("web/package-lock.json")
    assert not is_ignored("src/index/x.c")


def test_fixes_and_refs():
    fixes, refs = parse_refs("Fixes #12, closes: #13. See #14 and owner/repo#99 and url#frag")
    assert fixes == [12, 13]
    assert refs == [12, 13, 14]


def test_git_log_maps_files_to_prs():
    log = "\n".join([
        "@@fix: plan a self-loop hop (#3102)", "", "src/execution_plan/plan.c", "tests/flow/test_x.py",
        "@@Merge pull request #7 from someone/branch", "", "docs/a.md",
        "@@direct push without a PR", "", "src/other.c",
        "@@chore: bump deps (#8)", "", "deps/lib/x.c", "package-lock.json",
    ])
    assert parse_git_log(log) == {
        3102: ["src/execution_plan/plan.c", "tests/flow/test_x.py"],
        7: ["docs/a.md"],
        8: [],
    }


def test_big_areas_split_into_their_subdirectories():
    from lore.areas import assign, plan_areas
    files = [f"graph/src/planner/f{i}.rs" for i in range(40)] + [f"graph/src/index/f{i}.rs" for i in range(40)] + \
            ["graph/src/lib.rs"] + [f"docs/p{i}.md" for i in range(5)] + [f"build/graphblas/x{i}.c" for i in range(99)]
    plan = plan_areas(files, minimum=30)
    assert assign("graph/src/planner/f1.rs", plan) == "graph/src/planner"
    assert assign("graph/src/lib.rs", plan) == "graph/src"   # files directly in a split area stay there
    assert assign("docs/p1.md", plan) == "docs"              # small areas stay as they are
    assert not any(a.startswith("build") for a in plan)      # copied-in libraries never become areas

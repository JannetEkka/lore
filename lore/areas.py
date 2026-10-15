"""Which area of the code a file belongs to, and which files to ignore."""
from .config import AREA_DEPTH

# Code a project copies in from elsewhere: who edits it says little about who knows the project.
VENDORED = ("deps/", "vendor/", "third_party/", "node_modules/", "external/", "build/")
IGNORED_NAMES = {"package-lock.json", "yarn.lock", "pnpm-lock.yaml", "poetry.lock", "Cargo.lock", "go.sum", "uv.lock"}

ROOT = "(root)"


def is_ignored(path: str) -> bool:
    name = path.rsplit("/", 1)[-1]
    return path.startswith(VENDORED) or name in IGNORED_NAMES


def area_for(path: str, depth: int = AREA_DEPTH) -> str:
    """src/index/index.c -> src/index; tests/flow/x.py -> tests/flow; README.md -> (root)."""
    dirs = path.split("/")[:-1]
    return "/".join(dirs[:depth]) if dirs else ROOT


def plan_areas(paths, depth=AREA_DEPTH, share=0.06, minimum=30, max_depth=4):
    """Choose a repo's areas from the files it has: start at `depth` directories, then split any
    area holding more than `share` of the files (and at least `minimum`) into its subdirectories.
    graph/src (173 files) becomes graph/src/<module>; a small area stays as it is."""
    paths = [p for p in set(paths) if not is_ignored(p)]
    limit = max(minimum, share * len(paths))
    areas = {area_for(p, depth) for p in paths}
    for d in range(depth + 1, max_depth + 1):
        counts = {}
        for p in paths:
            a = assign(p, areas)
            counts[a] = counts.get(a, 0) + 1
        big = {a for a, n in counts.items() if n > limit and a != ROOT}
        if not big:
            break
        for p in paths:
            a = assign(p, areas)
            if a in big and len(p.split("/")) - 1 >= d:
                areas.add(area_for(p, d))
    return areas


def assign(path, areas):
    """The deepest planned area that contains the path."""
    dirs = path.split("/")[:-1]
    for d in range(len(dirs), 0, -1):
        prefix = "/".join(dirs[:d])
        if prefix in areas:
            return prefix
    return ROOT

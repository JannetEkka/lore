"""Which area of the code a file belongs to, and which files to ignore."""
from .config import AREA_DEPTH

# Code a project copies in from elsewhere: who edits it says little about who knows the project.
VENDORED = ("deps/", "vendor/", "third_party/", "node_modules/", "external/")
IGNORED_NAMES = {"package-lock.json", "yarn.lock", "pnpm-lock.yaml", "poetry.lock", "Cargo.lock", "go.sum", "uv.lock"}

ROOT = "(root)"


def is_ignored(path: str) -> bool:
    name = path.rsplit("/", 1)[-1]
    return path.startswith(VENDORED) or name in IGNORED_NAMES


def area_for(path: str, depth: int = AREA_DEPTH) -> str:
    """src/index/index.c -> src/index; tests/flow/x.py -> tests/flow; README.md -> (root)."""
    dirs = path.split("/")[:-1]
    return "/".join(dirs[:depth]) if dirs else ROOT

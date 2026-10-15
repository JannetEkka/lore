"""Connect to FalkorDB: a server if FALKORDB_HOST is set, otherwise an embedded one (FalkorDBLite)."""
import os

_embedded = {}


def connect(path=None):
    host = os.environ.get("FALKORDB_HOST")
    if host:
        if "server" not in _embedded:
            from falkordb import FalkorDB
            _embedded["server"] = FalkorDB(
                host=host, port=int(os.environ.get("FALKORDB_PORT", 6379)),
                username=os.environ.get("FALKORDB_USERNAME") or None, password=os.environ.get("FALKORDB_PASSWORD"),
                ssl=os.environ.get("FALKORDB_SSL", "").lower() in ("1", "true", "yes"))
        return _embedded["server"]
    from redislite.falkordb_client import FalkorDB
    path = path or os.environ.get("LORE_DB", os.path.join(os.path.dirname(__file__), "..", "data", "lore.rdb"))
    path = os.path.abspath(path)
    if path not in _embedded:
        os.makedirs(os.path.dirname(path), exist_ok=True)
        _embedded[path] = FalkorDB(path)
    return _embedded[path]


def graph_name(target):
    """One graph per org or repo (FalkorDB's multi-graph): "FalkorDB" or "FalkorDB__FalkorDB"."""
    return target.replace("/", "__")

"""Connect to FalkorDB: a server if FALKORDB_HOST is set, otherwise an embedded one (FalkorDBLite)."""
import os

_embedded = {}


def connect(path=None):
    host = os.environ.get("FALKORDB_HOST")
    if host:
        if "server" not in _embedded:
            from falkordb import FalkorDB
            try:
                conn = FalkorDB(
                    host=host, port=int(os.environ.get("FALKORDB_PORT", 6379)),
                    username=os.environ.get("FALKORDB_USERNAME") or None, password=os.environ.get("FALKORDB_PASSWORD"),
                    ssl=os.environ.get("FALKORDB_SSL", "").lower() in ("1", "true", "yes"), socket_connect_timeout=10)
                conn.connection.ping()
                _embedded["server"] = conn
            except Exception as e:  # a wrong host or password must not take the app down: use the embedded one
                print(f"FalkorDB server {host} unreachable ({e}); using embedded FalkorDB instead", flush=True)
                _embedded["server"] = None
        if _embedded["server"] is not None:
            return _embedded["server"]
    from redislite.falkordb_client import FalkorDB
    path = path or os.environ.get("LORE_DB", os.path.join(os.path.dirname(__file__), "..", "data", "lore.rdb"))
    path = os.path.abspath(path)
    if path not in _embedded:
        os.makedirs(os.path.dirname(path), exist_ok=True)
        _embedded[path] = FalkorDB(path)
    return _embedded[path]


def is_remote():
    """True when graphs live on a FalkorDB server (they survive restarts), False when embedded."""
    return bool(os.environ.get("FALKORDB_HOST")) and connect() is _embedded.get("server") and _embedded.get("server") is not None


def graph_name(target):
    """One graph per org or repo (FalkorDB's multi-graph): "FalkorDB" or "FalkorDB__FalkorDB"."""
    return target.replace("/", "__")

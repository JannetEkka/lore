"""Lore as an MCP server: any MCP client (Claude Code, Cursor, another agent) can call Lore's graph tools.

  pip install "mcp>=2"
  python -m lore.mcp_server            # stdio; loads data/snapshots/*.json.gz into embedded FalkorDB if needed

Claude Code:  claude mcp add lore -- python -m lore.mcp_server   (run from this repo)
Set FALKORDB_HOST to use a FalkorDB server instead of the embedded one.
"""
import glob
import os

from mcp.server.mcpserver import MCPServer

from . import coverage, db, queries
from .__main__ import read_snapshot
from .load import load_snapshot

ROOT = os.path.abspath(os.path.join(os.path.dirname(__file__), ".."))
mcp = MCPServer("lore")


def _snapshot(project):
    for path in glob.glob(os.path.join(ROOT, "data", "snapshots", "*.json*")):
        snap = read_snapshot(path)
        if db.graph_name(snap["target"]) == db.graph_name(project):
            return snap
    return None


def _coverage(project):
    snap = _snapshot(project)
    return coverage.measure(snap) if snap else {"partial": True, "summary": "No snapshot on disk, so how much of the history was read is unknown", "details": []}


def _graph(project):
    g = db.connect().select_graph(db.graph_name(project))
    try:
        if g.query("MATCH (n:Person) RETURN count(n)").result_set[0][0]:
            return g
    except Exception:
        pass
    for path in glob.glob(os.path.join(ROOT, "data", "snapshots", "*.json*")):
        snap = read_snapshot(path)
        if db.graph_name(snap["target"]) == db.graph_name(project):
            load_snapshot(g, snap)
            return g
    raise ValueError(f"No graph or snapshot for {project}. Fetch it first: python -m lore fetch {project}")


@mcp.tool()
def who_knows(project: str, topic: str, active_only: bool = False) -> list:
    """People who know an area of the project's code (by merged PRs, reviews and comments, recent work counting
    more), each with the PRs that prove it. project: an org like 'FalkorDB' or 'owner/repo'. topic: e.g. 'index'."""
    g = _graph(project)
    areas = [a["area"] for a in queries.find_areas(g, topic)]
    return queries.who_knows(g, topic, areas=areas, active_only=active_only)


@mcp.tool()
def related_issues(project: str, issue: str) -> dict:
    """Past issues and PRs related to an issue ('owner/repo#123' or '#123'), each with the reason, plus its areas."""
    g = _graph(project)
    return queries.related(g, queries.resolve_item(g, issue))


@mcp.tool()
def suggest_reviewers(project: str, issue: str) -> dict:
    """Active people who know an issue's areas best (never its author), with evidence."""
    g = _graph(project)
    return queries.suggest_reviewers(g, queries.resolve_item(g, issue))


@mcp.tool()
def search_history(project: str, text: str) -> list:
    """Full-text search over the project's PR and issue titles and descriptions: where something was discussed."""
    return queries.search_history(_graph(project), text)


@mcp.tool()
def knowledge_risk(project: str) -> dict:
    """Areas with recent work that at most one active person knows well IN THE FETCHED DATA. Read `coverage`
    first: when it is partial, say "based on the fetched data" and never state a bus factor of one as fact."""
    cov = _coverage(project)
    return {"coverage": {k: cov[k] for k in ("partial", "summary", "details")}, "areas": queries.knowledge_risk(_graph(project))}


@mcp.tool()
def data_coverage(project: str) -> dict:
    """How much of the project's history Lore read: merged PRs with known files, review approvals, caps hit.
    When partial, people outside the fetched data are invisible, so no answer proves an absence."""
    return _coverage(project)


@mcp.tool()
def teams(project: str) -> list:
    """The teams that actually work together (community detection over who reviews whom), with central people."""
    return queries.teams(_graph(project))


if __name__ == "__main__":
    mcp.run()

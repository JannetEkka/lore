# Lore

**What an open-source project knows but never wrote down.**

Lore turns a public GitHub project's history (PRs, reviews, issues, comments, and the files each PR changed) into a graph in [FalkorDB](https://github.com/FalkorDB/FalkorDB), then answers from it: who knows this part of the code, which past issues relate to a new one, who should look at it, and which parts only one active person knows. Every answer shows the PRs behind it and the Cypher that produced it.

Built for [Graph Hacks](https://wemakedevs.org/hackathons/falkordb) (FalkorDB × WeMakeDevs), Oct 15–18, 2026. Tracks: Company Brain, and Agents That Act on Connected Data.

**Status:** day 1. Data in, graph built, four questions answered with evidence. The agent, the web app and the live link come next.

## Try it

```bash
python -m venv .venv && . .venv/bin/activate
pip install -r requirements.txt
python -m pytest -q
python -m lore fetch FalkorDB/FalkorDB        # export GITHUB_TOKEN=... for 5,000 calls an hour and approvals
python -m lore load data/FalkorDB__FalkorDB.json
python -m lore who-knows FalkorDB/FalkorDB index --trace
python -m lore reviewers FalkorDB/FalkorDB "FalkorDB/FalkorDB#1234"
python -m lore risk FalkorDB/FalkorDB
```

FalkorDB runs embedded (FalkorDBLite) unless `FALKORDB_HOST` points at a server.

## How it works

See [EXPLAIN.md](EXPLAIN.md): the data sources, the graph model, the scoring rule, every query, and the limits.

## Data

Public GitHub activity only. Lore keeps GitHub logins, never emails or names, and skips bots.

## Built with AI

Built with Claude Code as the coding assistant. Jannet Ekka chose the problem, the graph model, the scoring rule and the product design, and reviews the code, tests and results.

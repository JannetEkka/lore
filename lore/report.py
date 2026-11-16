"""A short "who knows what" report for one project, from a snapshot: no model, no database.

Used for outreach: run Lore on a project before writing to its maintainers, so the first message
carries findings about their own code. Every number comes from the snapshot's merged PRs and
reviews in the window, and each finding names the PRs it rests on.

    python -m lore.report data/outreach/owner__repo.json.gz --months 6
"""
import argparse
import collections
import datetime
import gzip
import json

from .areas import assign, is_ignored, plan_areas


def _ts(v):
    if v is None:
        return None
    if isinstance(v, (int, float)):
        return float(v)
    return datetime.datetime.fromisoformat(v.replace("Z", "+00:00")).timestamp()


def _is_bot(login):
    return not login or login.endswith("[bot]") or login.endswith("-bot")


def summarise(snap, months=6, top=5):
    """One dict per repo: merged PRs, people, and the areas that rest on one person."""
    cut = snap["fetched_at"] - months * 30.5 * 86400
    out = []
    for repo in snap["repos"]:
        reviewers = collections.defaultdict(set)
        for rv in repo.get("reviews", []):
            if not _is_bot(rv.get("author")):
                reviewers[rv.get("pr")].add(rv["author"])
        merged = [p for p in repo.get("prs", [])
                  if p.get("merged_at") and _ts(p["merged_at"]) >= cut and not _is_bot(p.get("author"))]
        areas = plan_areas([f for p in repo.get("prs", []) for f in (p.get("files") or [])])
        people = collections.defaultdict(collections.Counter)   # area -> login -> PRs
        prs_in = collections.defaultdict(list)                   # area -> PR numbers
        wrote = collections.defaultdict(collections.Counter)     # area -> author -> PRs
        authors = collections.Counter()
        for p in merged:
            authors[p["author"]] += 1
            who = {p["author"]} | reviewers.get(p["number"], set())
            for a in {assign(f, areas) for f in (p.get("files") or []) if not is_ignored(f)}:
                prs_in[a].append(p["number"])
                wrote[a][p["author"]] += 1
                for w in who:
                    people[a][w] += 1
        single = []
        for a, c in people.items():
            n = len(set(prs_in[a]))
            if n >= 3 and len(c) == 1:
                single.append({"area": a, "prs": n, "person": next(iter(c)),
                               "examples": sorted(set(prs_in[a]))[-3:]})
        single.sort(key=lambda r: -r["prs"])
        mostly = []   # areas where one author wrote at least 80% of 5+ merged PRs
        for a, c in wrote.items():
            n = sum(c.values())
            who, k = c.most_common(1)[0]
            if n >= 5 and k / n >= 0.8 and a != "(root)":
                mostly.append({"area": a, "prs": n, "person": who, "wrote": k})
        mostly.sort(key=lambda r: -r["prs"])
        busiest = sorted(people.items(), key=lambda kv: -len(set(prs_in[kv[0]])))[:top]
        open_issues = [i for i in repo.get("issues", []) if i.get("state") == "open"]
        out.append({
            "repo": repo["name"],
            "merged_prs": len(merged),
            "authors": len(authors),
            "top_authors": authors.most_common(3),
            "single_person_areas": single[:top],
            "one_author_areas": mostly[:top],
            "busiest_areas": [{"area": a, "prs": len(set(prs_in[a])), "people": c.most_common(3)}
                              for a, c in busiest],
            "open_issues": len(open_issues),
            "has_approvals": snap.get("has_approvals", False),
            "incomplete": snap.get("incomplete", []),
        })
    return out


def to_markdown(rows, months):
    lines = []
    for r in rows:
        lines.append(f"## {r['repo']}")
        lines.append(f"- Last {months} months: {r['merged_prs']} merged PRs by {r['authors']} people; "
                     f"open issues in the snapshot: {r['open_issues']}.")
        if r["top_authors"] and r["merged_prs"]:
            share = sum(n for _, n in r["top_authors"][:2]) / r["merged_prs"]
            names = " and ".join(f"@{a} ({n})" for a, n in r["top_authors"][:2])
            lines.append(f"- {names} authored {share:.0%} of them.")
        if r["single_person_areas"]:
            lines.append("- Areas where every merged PR in the window was written and reviewed by one person:")
            for s in r["single_person_areas"]:
                ex = ", ".join(f"#{n}" for n in s["examples"])
                lines.append(f"  - `{s['area']}`: {s['prs']} PRs, all @{s['person']} (e.g. {ex})")
        else:
            lines.append("- No area with 3+ merged PRs rests on a single person in the window.")
        if r["one_author_areas"]:
            lines.append("- Areas where one person wrote at least 80% of the merged PRs:")
            for m in r["one_author_areas"]:
                lines.append(f"  - `{m['area']}`: @{m['person']} wrote {m['wrote']} of {m['prs']}")
        lines.append("- Busiest areas: " + "; ".join(
            f"`{b['area']}` {b['prs']} PRs ({', '.join('@' + p for p, _ in b['people'])})"
            for b in r["busiest_areas"]))
        if not r["has_approvals"]:
            lines.append("- Note: fetched without review approvals, so reviewers are counted from review comments only.")
        if r["incomplete"]:
            lines.append(f"- Partial data: {', '.join(r['incomplete'])}.")
        lines.append("")
    return "\n".join(lines)


def main(argv=None):
    ap = argparse.ArgumentParser(prog="lore.report")
    ap.add_argument("snapshots", nargs="+")
    ap.add_argument("--months", type=int, default=6)
    ap.add_argument("--json", action="store_true")
    a = ap.parse_args(argv)
    rows = []
    for path in a.snapshots:
        opener = gzip.open if path.endswith(".gz") else open
        with opener(path, "rt") as fh:
            rows += summarise(json.load(fh), a.months)
    print(json.dumps(rows, indent=1) if a.json else to_markdown(rows, a.months))


if __name__ == "__main__":
    main()

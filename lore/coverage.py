"""How much of a project's history a snapshot holds, and what to say when it isn't all of it.

Lore's answers about who knows what come only from what was fetched: merged PRs whose files are known, reviews
and comments. When a fetch hits one of its caps (lore/config.py Caps), GitHub's rate limit, or PRs that git can't
tie to files, people outside that data are invisible, so "only @x knows this" can be an artefact of the fetch.
Anna's App Review of Lore for Anna (Oct 28, 2026) found exactly this; this module is the same fix for this repo.

measure(snapshot) works on snapshots from before the fetcher recorded its limits too: it falls back to the
default caps and infers what it can from the data.
"""
import re
import time

from .config import Caps

_DEFAULT = Caps()


def _plural(n, word):
    return f"{n:,} {word}{'' if n == 1 else 's'}"


def _date(epoch):
    return time.strftime("%b %-d, %Y", time.gmtime(epoch))


def _join(names):
    names = list(names)
    return names[0] if len(names) == 1 else ", ".join(names[:-1]) + " and " + names[-1]


def measure(snap):
    """A dict: partial (bool), summary (one sentence, "" when complete), details (per-repo lines), and counts."""
    caps = {**_DEFAULT.__dict__, **(snap.get("caps") or {})}
    has_approvals = bool(snap.get("has_approvals"))
    repos = []
    for r in snap.get("repos", []):
        prs = r.get("prs", [])
        merged = [p for p in prs if p.get("state") == "merged"]
        lim = r.get("limits")
        if lim is None:   # an older snapshot: infer the caps it would have hit
            lim = {"pr_cap_hit": len(prs) >= caps["max_prs"], "issue_cap_hit": len(r.get("issues", [])) >= caps["max_issues"],
                   "comment_cap_hit": len(r.get("comments", [])) >= caps["max_comment_pages"] * 100,
                   "review_comment_cap_hit": False,
                   "approvals_for": min(len(merged), caps["max_review_calls"]) if has_approvals else 0}
        created = [p["created_at"] for p in prs if p.get("created_at")]
        repos.append({
            "repo": r["name"], "prs": len(prs), "merged": len(merged),
            "files_for": sum(1 for p in merged if p.get("files")),
            "approvals_for": min(lim.get("approvals_for", 0), len(merged)) if has_approvals else 0,
            "prs_from": min(created) if created else None,
            "pr_cap_hit": bool(lim.get("pr_cap_hit")), "issue_cap_hit": bool(lim.get("issue_cap_hit")),
            "comment_cap_hit": bool(lim.get("comment_cap_hit") or lim.get("review_comment_cap_hit")),
        })

    merged = sum(r["merged"] for r in repos)
    files_for = sum(r["files_for"] for r in repos)
    approvals_for = sum(r["approvals_for"] for r in repos)
    incomplete = list(snap.get("incomplete", []))
    org_total = snap.get("org_repos_total")
    months = caps["months"]

    parts, details = [], []
    if incomplete:
        parts.append(f"GitHub's limit stopped {_join(incomplete)} before it was read")
    if files_for < merged:
        parts.append(f"files are known for {files_for:,} of {_plural(merged, 'merged PR')}")
    if merged and not has_approvals:
        parts.append("no review approvals were read (fetched without a GitHub token)")
    elif approvals_for < merged:
        parts.append(f"review approvals were read for {approvals_for:,} of them")
    capped = [r for r in repos if r["pr_cap_hit"]]
    if capped:
        parts.append(f"{_join(r['repo'] for r in capped)} {'goes' if len(capped) == 1 else 'go'} back only "
                     f"{caps['max_prs']:,} PRs, not the full {months} months")
    if any(r["issue_cap_hit"] for r in repos):
        parts.append(f"only the {caps['max_issues']:,} newest issues were read in "
                     f"{_join(r['repo'] for r in repos if r['issue_cap_hit'])}")
    if any(r["comment_cap_hit"] for r in repos):
        parts.append(f"comments were cut short in {_join(r['repo'] for r in repos if r['comment_cap_hit'])}")
    if org_total and org_total > len(repos) + len(incomplete):
        parts.append(f"{len(repos)} of the org's {org_total} public repos were read")

    for r in repos:
        bits = []
        if r["files_for"] < r["merged"]:
            bits.append(f"files for {r['files_for']:,} of {r['merged']:,} merged PRs")
        if has_approvals and r["approvals_for"] < r["merged"]:
            bits.append(f"approvals for {r['approvals_for']:,}")
        if r["pr_cap_hit"] and r["prs_from"]:
            bits.append(f"PRs back to {_date(r['prs_from'])} only")
        if r["issue_cap_hit"]:
            bits.append("newest issues only")
        if r["comment_cap_hit"]:
            bits.append("comments cut short")
        if bits:
            details.append(f"{r['repo']}: " + ", ".join(bits))

    summary = "; ".join(parts)
    return {"partial": bool(parts), "summary": summary[:1].upper() + summary[1:] if summary else "",
            "details": details, "merged_prs": merged, "files_for": files_for, "approvals_for": approvals_for,
            "has_approvals": has_approvals, "incomplete": incomplete, "repos": repos}


def data_note(cov):
    """A paragraph for the model's instructions when the data is partial; "" when it is complete."""
    if not cov or not cov.get("partial"):
        return ""
    return f"""The data is PARTIAL: {cov['summary']}.
People and pull requests that were not fetched are invisible to you, so nothing here proves an absence.
- Start any statement about who knows an area, bus factor or knowledge risk with "Based on the fetched data".
- Never state as fact that only one person knows something, that nobody else does, or that an area has a bus factor of one. Say what the fetched data shows, and that the full history may show more people.
- In a drafted reply to an issue, say the people named know the area well in the history Lore read; never call anyone the only one.
- Say once, briefly, that the answer rests on partial data."""


HEDGE_RE = re.compile(r"\b(fetched|partial|loaded|available|incomplete)\s+(data|history)\b|\bdata\s+(is|was)\s+(partial|incomplete)\b"
                      r"|\bmay\s+(show|be|have)\b|\bappears?\b", re.I)
ABSOLUTE_RES = [re.compile(p, re.I) for p in (
    r"\bonly\s+(?:\*\*)?@?[\w-]+(?:\*\*)?\s+(?:really\s+|actively\s+|currently\s+)?(?:knows?|maintains?|understands?|works?\s+on|owns?|reviews?|touche[sd]|has)\b",
    r"\b(?:the\s+)?only\s+(?:one\s+)?(?:active\s+)?(?:person|maintainer|contributor|expert|developer|reviewer|one)\b",
    r"\b(?:nobody|no\s+one|no\s+other\s+\w+|none\s+of\s+the\s+others?)\s+(?:else\s+)?(?:knows?|maintains?|understands?|works?|has|is|reviews?|owns?)\b",
    r"\b(?:nobody|no\s+one)\s+else\b",
    r"\bsole(?:ly)?\s+(?:\w+\s+)?(?:maintainer|expert|owner|contributor|author|responsible|maintained|owned)\b",
    r"\bsingle\s+point\s+of\s+failure\b",
    r"\bbus\s+factor\s+(?:of|is|=|:)\s*(?:one|1)\b",
    r"\b(?:depends?|relies|rely|dependent)\s+(?:entirely|solely|completely|only)\s+on\b",
)]


def absolute_claims(cov, text):
    """Sentences that state an absence or a bus factor as fact without saying the data is partial.
    [] when the data is complete or every such sentence is hedged."""
    if not cov or not cov.get("partial"):
        return []
    found = []
    for raw in re.split(r"(?<=[.!?])\s+|\n+", text or ""):
        sentence = re.sub(r"^[\s>*-]+", "", raw).strip()
        if not sentence or HEDGE_RE.search(sentence):
            continue
        if any(r.search(sentence) for r in ABSOLUTE_RES):
            found.append(sentence if len(sentence) <= 160 else sentence[:157] + "…")
    return found

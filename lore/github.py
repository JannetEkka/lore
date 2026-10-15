"""Fetch a public GitHub org or repo into a snapshot: plain JSON that load.py turns into a graph.

Two sources, so it works without a token:

- GitHub's REST API, bulk endpoints only (100 items a call): PRs, issues, review comments and
  issue comments. Without a token GitHub allows 60 calls an hour, with GITHUB_TOKEN 5,000. With a
  token, each merged PR's reviews (approvals included) are fetched too.
- git: a blobless clone gives the files each merged PR changed, for no API calls at all.

Only public data is read, and only GitHub logins are kept: no emails, no names.
"""
import hashlib
import json
import os
import re
import subprocess
import time
from datetime import datetime, timedelta, timezone

import requests

from .areas import is_ignored
from .config import Caps

API = "https://api.github.com"

# "Fixes #12", "closes: #12", "Resolved #12"
FIX_RE = re.compile(r"\b(?:fix(?:e[sd])?|close[sd]?|resolve[sd]?)\s*:?\s+#(\d+)", re.I)
# Any "#12" that isn't part of a word, a URL fragment or "owner/repo#12"
REF_RE = re.compile(r"(?<![\w/#&])#(\d+)\b")
# Squash merge "Title (#123)" or merge commit "Merge pull request #123 from ..."
PR_SUBJECT_RE = re.compile(r"\(#(\d+)\)\s*$|^Merge pull request #(\d+)")

BOT_LOGINS = {"dependabot", "github-actions", "renovate", "codecov", "coderabbitai", "copilot"}


class RateLimited(Exception):
    """GitHub's hourly allowance is used up. Cached pages are kept, so a re-run resumes."""


class Refused(Exception):
    """GitHub (or a proxy in front of it) refused the request: private repo, no access, bad token."""


def is_bot(user) -> bool:
    if not user:
        return True
    login = user.get("login", "")
    return user.get("type") == "Bot" or login.endswith("[bot]") or login.lower() in BOT_LOGINS


def iso_to_epoch(s):
    return int(datetime.fromisoformat(s.replace("Z", "+00:00")).timestamp()) if s else None


def parse_refs(text):
    """Issue/PR numbers a text fixes, and every number it mentions."""
    text = text or ""
    fixes = {int(n) for n in FIX_RE.findall(text)}
    refs = {int(n) for n in REF_RE.findall(text)}
    return sorted(fixes), sorted(refs)


def parse_git_log(text):
    """Parse `git log --first-parent -m --name-only --format=@@%s` into {pr_number: [files]}."""
    files, current = {}, None
    for line in text.splitlines():
        if line.startswith("@@"):
            m = PR_SUBJECT_RE.search(line[2:])
            current = int(m.group(1) or m.group(2)) if m else None
            if current is not None:
                files.setdefault(current, [])
        elif line.strip() and current is not None and not is_ignored(line.strip()):
            if line.strip() not in files[current]:
                files[current].append(line.strip())
    return files


class GitHub:
    def __init__(self, token=None, cache_dir=None, cache_hours=12):
        self.session = requests.Session()
        self.session.headers["Accept"] = "application/vnd.github+json"
        if token:
            self.session.headers["Authorization"] = f"Bearer {token}"
        self.has_token = bool(token)
        self.cache_dir = cache_dir
        self.cache_seconds = cache_hours * 3600
        self.calls = 0
        self.remaining = None
        if cache_dir:
            os.makedirs(cache_dir, exist_ok=True)

    def _cache_path(self, url, params):
        key = hashlib.sha1((url + json.dumps(params or {}, sort_keys=True)).encode()).hexdigest()
        return os.path.join(self.cache_dir, key + ".json")

    def get(self, path, params=None):
        url = path if path.startswith("http") else API + path
        cached = self._cache_path(url, params) if self.cache_dir else None
        if cached and os.path.exists(cached) and time.time() - os.path.getmtime(cached) < self.cache_seconds:
            with open(cached) as f:
                return json.load(f)
        if self.remaining == 0:
            raise RateLimited("GitHub's hourly allowance is used up")
        r = self.session.get(url, params=params, timeout=30)
        self.calls += 1
        self.remaining = int(r.headers.get("X-RateLimit-Remaining", "1"))
        if r.status_code in (403, 429) and self.remaining == 0:
            raise RateLimited("GitHub's hourly allowance is used up")
        if r.status_code in (401, 403, 404):
            try:
                message = r.json().get("message", "")
            except ValueError:
                message = r.text[:200]
            raise Refused(f"GitHub refused {url} ({r.status_code}): {message}")
        r.raise_for_status()
        data = r.json()
        if cached:
            with open(cached, "w") as f:
                json.dump(data, f)
        return data

    def pages(self, path, params, max_pages=50):
        for page in range(1, max_pages + 1):
            batch = self.get(path, {**params, "per_page": 100, "page": page})
            yield from batch
            if len(batch) < 100:
                return


def org_repos(gh, org, caps):
    """The org's most recently pushed public repos, without forks or archived ones."""
    try:
        repos = list(gh.pages(f"/orgs/{org}/repos", {"type": "public", "sort": "pushed"}, max_pages=2))
    except Refused:
        repos = list(gh.pages(f"/users/{org}/repos", {"sort": "pushed"}, max_pages=2))
    repos = [r for r in repos if not r.get("fork") and not r.get("archived")]
    repos.sort(key=lambda r: r.get("pushed_at") or "", reverse=True)
    return [r["full_name"] for r in repos[: caps.max_repos]]


def git_pr_files(full, since, git_dir):
    """Files changed by each merged PR on the default branch, from a blobless clone."""
    path = os.path.join(git_dir, full.replace("/", "__"))
    if not os.path.isdir(path):
        os.makedirs(git_dir, exist_ok=True)
        subprocess.run(
            ["git", "clone", "-q", "--filter=blob:none", "--no-checkout",
             f"--shallow-since={since.date().isoformat()}", f"https://github.com/{full}", path],
            check=True, env={**os.environ, "GIT_LFS_SKIP_SMUDGE": "1"}, timeout=900)
    log = subprocess.run(
        ["git", "-C", path, "log", "--first-parent", "-m", "--name-only", "--format=@@%s",
         f"--since={since.date().isoformat()}", "HEAD"],
        check=True, capture_output=True, text=True, timeout=300).stdout
    return parse_git_log(log)


def fetch_repo(gh, full, since, caps, git_dir, log=print):
    since_iso = since.strftime("%Y-%m-%dT%H:%M:%SZ")
    url = f"https://github.com/{full}"

    prs = []
    for p in gh.pages(f"/repos/{full}/pulls", {"state": "all", "sort": "created", "direction": "desc"}):
        if p["created_at"] < since_iso or len(prs) >= caps.max_prs:
            break
        if is_bot(p["user"]):
            continue
        fixes, refs = parse_refs(f"{p['title']}\n{p.get('body') or ''}")
        prs.append({
            "number": p["number"], "title": p["title"], "body": (p.get("body") or "")[:1500],
            "author": p["user"]["login"],
            "state": "merged" if p.get("merged_at") else p["state"],
            "created_at": iso_to_epoch(p["created_at"]), "merged_at": iso_to_epoch(p.get("merged_at")),
            "labels": [l["name"] for l in p.get("labels", [])],
            "fixes": fixes, "refs": [n for n in refs if n != p["number"]], "url": p["html_url"],
        })
    log(f"  {full}: {len(prs)} PRs")

    issues = []
    for i in gh.pages(f"/repos/{full}/issues", {"state": "all", "sort": "created", "direction": "desc"}):
        if i["created_at"] < since_iso or len(issues) >= caps.max_issues:
            break
        if "pull_request" in i or is_bot(i["user"]):
            continue
        body = (i.get("body") or "")[:2000]
        _, refs = parse_refs(f"{i['title']}\n{body}")
        issues.append({
            "number": i["number"], "title": i["title"], "body": body, "author": i["user"]["login"],
            "state": i["state"], "created_at": iso_to_epoch(i["created_at"]),
            "closed_at": iso_to_epoch(i.get("closed_at")),
            "labels": [l["name"] for l in i.get("labels", [])],
            "refs": [n for n in refs if n != i["number"]], "url": i["html_url"],
        })
    log(f"  {full}: {len(issues)} issues")

    # Conversation comments on issues and PRs (one endpoint covers both: they share numbers).
    comments = []
    for c in gh.pages(f"/repos/{full}/issues/comments", {"since": since_iso, "sort": "created", "direction": "desc"},
                      max_pages=caps.max_comment_pages):
        if not is_bot(c["user"]):
            comments.append({"number": int(c["issue_url"].rsplit("/", 1)[1]), "author": c["user"]["login"],
                             "at": iso_to_epoch(c["created_at"])})

    # Review comments on PR diffs: each one means that person reviewed that PR.
    reviews = []
    for c in gh.pages(f"/repos/{full}/pulls/comments", {"since": since_iso, "sort": "created", "direction": "desc"},
                      max_pages=caps.max_comment_pages):
        if not is_bot(c["user"]):
            reviews.append({"pr": int(c["pull_request_url"].rsplit("/", 1)[1]), "author": c["user"]["login"],
                            "state": "COMMENTED", "at": iso_to_epoch(c["created_at"])})

    # Approvals and change requests have no bulk endpoint: one call per merged PR, so token only.
    if gh.has_token:
        for p in [p for p in prs if p["state"] == "merged"][: caps.max_review_calls]:
            for r in gh.get(f"/repos/{full}/pulls/{p['number']}/reviews", {"per_page": 100}):
                if not is_bot(r.get("user")) and r["user"]["login"] != p["author"] and r.get("submitted_at"):
                    reviews.append({"pr": p["number"], "author": r["user"]["login"], "state": r["state"],
                                    "at": iso_to_epoch(r["submitted_at"])})
    log(f"  {full}: {len(comments)} comments, {len(reviews)} review records")

    files = git_pr_files(full, since, git_dir)
    for p in prs:
        p["files"] = files.get(p["number"], [])
    log(f"  {full}: files for {sum(1 for p in prs if p['files'])} merged PRs from git")

    return {"name": full, "url": url, "prs": prs, "issues": issues, "comments": comments, "reviews": reviews}


def fetch(target, data_dir, token=None, caps=None, repos=None, log=print):
    """target is an org ("FalkorDB") or a repo ("FalkorDB/FalkorDB"). Returns the snapshot dict."""
    caps = caps or Caps()
    gh = GitHub(token, cache_dir=os.path.join(data_dir, "cache"))
    since = datetime.now(timezone.utc) - timedelta(days=30 * caps.months)
    if repos is None:
        repos = [target] if "/" in target else org_repos(gh, target, caps)
    log(f"Fetching {len(repos)} repo(s) since {since.date()} ({'token' if token else 'no token: 60 calls/hour'})")
    snapshot = {
        "target": target, "since": int(since.timestamp()), "fetched_at": int(time.time()),
        "has_approvals": gh.has_token, "repos": [], "incomplete": [],
    }
    for full in repos:
        try:
            snapshot["repos"].append(fetch_repo(gh, full, since, caps, os.path.join(data_dir, "git"), log))
        except RateLimited:
            snapshot["incomplete"].append(full)
            log(f"  {full}: GitHub's hourly allowance ran out; re-run later to finish (pages so far are cached)")
        except Refused as e:
            snapshot["incomplete"].append(full)
            log(f"  {full}: skipped. {e}")
    log(f"Done: {gh.calls} API calls, {gh.remaining} left this hour")
    return snapshot

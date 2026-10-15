"""Every number Lore's answers depend on, in one place."""
import math
from dataclasses import dataclass

# How much one piece of work in an area counts towards KNOWS(person, area).
W_AUTHOR = 1.0    # wrote a PR that was merged
W_REVIEW = 0.6    # reviewed a PR (approval, change request or review comment)
W_COMMENT = 0.2   # commented on a PR, or on an issue a PR then fixed

HALF_LIFE_DAYS = 180   # work this old counts half
ACTIVE_DAYS = 90       # "active" = any activity in the project in this many days
TEAM_MIN_REVIEWS = 5   # two people "work closely" after this many reviews between them (Teams view)
AREA_DEPTH = 2         # an area is the first two directories of a path: src/index/x.c -> src/index

# Exponent for the recency decay: weight * exp(DECAY_K * age_in_seconds) halves every HALF_LIFE_DAYS.
DECAY_K = -math.log(2) / (HALF_LIFE_DAYS * 86400)


@dataclass
class Caps:
    """Size caps so a build stays within a few minutes and one hour of GitHub's allowance."""
    months: int = 12
    max_repos: int = 10
    max_prs: int = 500         # per repo
    max_issues: int = 500      # per repo
    max_comment_pages: int = 10  # 100 comments a page, per repo and comment kind
    max_review_calls: int = 150  # per repo, with a token: approvals for the most recent merged PRs

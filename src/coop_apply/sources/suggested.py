"""A starter list of companies whose public job boards often carry intern / co-op roles.

Board names are the slug in each company's careers URL. Companies move between job
platforms, so some may go stale; fetching logs and skips any board that isn't found.
"""

SUGGESTED_COMPANIES: dict[str, list[str]] = {
    "greenhouse": [
        "stripe", "databricks", "figma", "discord", "robinhood", "airbnb", "dropbox", "pinterest",
        "lyft", "reddit", "asana", "cloudflare", "datadog", "gitlab", "instacart", "roblox",
        "twitch", "duolingo", "affirm", "brex", "scaleai", "anthropic", "faire", "coinbase",
        "samsara", "okta", "mongodb", "elastic", "squarespace", "etsy",
    ],
    "lever": ["palantir", "spotify", "wealthsimple"],
    "ashby": ["cohere", "openai", "ramp", "notion", "linear"],
}


def merge_suggested(current: dict[str, list[str]]) -> tuple[dict[str, list[str]], int]:
    """Add suggested boards to the current ones; returns (merged, number added)."""
    merged, added = {}, 0
    for kind, boards in SUGGESTED_COMPANIES.items():
        existing = list(current.get(kind, []))
        for b in boards:
            if b not in existing:
                existing.append(b)
                added += 1
        merged[kind] = existing
    return merged, added

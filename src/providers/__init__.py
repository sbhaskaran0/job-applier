"""Job-search providers. The watchlist provider pulls live roles from companies'
public ATS board APIs. The seam (get_provider) is kept so another provider (e.g.
an Apify discovery pass) could be added later without touching the skill."""

from . import watchlist


def get_provider(name: str = "watchlist"):
    if name == "watchlist":
        return watchlist
    raise ValueError(f"Unknown search provider: {name}")

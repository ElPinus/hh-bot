"""Loader for the local per-variant feed config (resume-variants/feeds.yaml).

Holds the SEMI-PERSONAL per-variant data the bot needs at runtime but that must
never reach the public repo: each variant's hh "similar vacancies" search URL
(carries the résumé hash) and the résumé title to pick in the apply modal. The
NON-secret variant identities live in ai/resume_variants.py.

The file is OPTIONAL. When it is absent or has no enabled feed the bot falls
back to the legacy single HH_RESUME_SEARCH_URL and hh's default apply résumé,
so everything keeps working before your résumé feeds are configured. Read per
call (not cached) so edits to feeds.yaml are picked up without a restart —
matching the project's per-call config-read convention (candidate.txt /
analyzer_summary).

See resume-variants/feeds.example.yaml for the format.
"""
import logging
from urllib.parse import urlparse, parse_qs

import yaml

from config import BASE_DIR
from ai.resume_variants import VARIANT_ORDER, normalize_key

logger = logging.getLogger(__name__)

FEEDS_PATH = BASE_DIR / "resume-variants" / "feeds.yaml"


def _load_raw() -> dict:
    if not FEEDS_PATH.exists():
        return {}
    try:
        data = yaml.safe_load(FEEDS_PATH.read_text(encoding="utf-8")) or {}
    except Exception as e:
        logger.warning("feeds.yaml parse failed (%s) - ignoring", e)
        return {}
    if not isinstance(data, dict):
        logger.warning("feeds.yaml is not a mapping - ignoring")
        return {}
    return data


def _variants_map() -> dict:
    """Per-variant entries keyed by string variant key (tolerates YAML int keys)."""
    variants = _load_raw().get("variants")
    if not isinstance(variants, dict):
        return {}
    return {str(k): v for k, v in variants.items()}


def default_variant() -> str:
    """feeds.yaml `default_variant`, else the registry DEFAULT_VARIANT."""
    return normalize_key(_load_raw().get("default_variant"))


def enabled_feeds() -> list[tuple[str, str]]:
    """[(variant_key, feed_url), ...] for enabled variants with a feed_url, in
    canonical registry order. Empty when feeds.yaml is absent/unconfigured (the
    caller then falls back to the legacy single feed)."""
    vmap = _variants_map()
    out: list[tuple[str, str]] = []
    for key in VARIANT_ORDER:
        entry = vmap.get(key)
        if not isinstance(entry, dict):
            continue
        if not entry.get("enabled", True):
            continue
        url = (entry.get("feed_url") or "").strip()
        if url:
            out.append((key, url))
    # Keyword feeds: hh text-searches (not résumé-similarity), each attached to
    # a `variant` so the apply-résumé + router hint reuse that variant's config.
    # Appended after the résumé feeds. Optional — absent section -> no change.
    # Lets you target a niche role cluster whose titles vary too much for
    # résumé-similarity to surface reliably.
    kfeeds = _load_raw().get("keyword_feeds")
    if isinstance(kfeeds, list):
        for kf in kfeeds:
            if not isinstance(kf, dict) or not kf.get("enabled", True):
                continue
            url = (kf.get("feed_url") or "").strip()
            if url:
                out.append((normalize_key(kf.get("variant")), url))
    return out


def apply_resume_for(variant_key: str | None) -> str | None:
    """hh résumé title to select in the apply modal for this variant
    (feeds.yaml `apply_resume`), or None when unconfigured — the apply flow then
    leaves hh's default résumé selected."""
    if not variant_key:
        return None
    entry = _variants_map().get(str(variant_key))
    if isinstance(entry, dict):
        return (entry.get("apply_resume") or "").strip() or None
    return None


def apply_resume_hash_for(variant_key: str | None) -> str | None:
    """The résumé hash (the `resume=` param of this variant's feed_url) to
    select in the apply modal. Preferred over the title — it is what hh puts on
    each picker option as data-magritte-select-option, and it survives renaming
    a résumé's title. Derived from feed_url so it can never desync from the
    feed. None when unconfigured."""
    if not variant_key:
        return None
    entry = _variants_map().get(str(variant_key))
    if not isinstance(entry, dict):
        return None
    url = (entry.get("feed_url") or "").strip()
    if not url:
        return None
    vals = parse_qs(urlparse(url).query).get("resume")
    return (vals[0].strip() or None) if vals else None

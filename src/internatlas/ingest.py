"""Automatic ingestion of live internship postings.

Two kinds of sources, both declared in ``automation/sources.yaml``:

**1. ATS boards (primary).** Public, keyless JSON APIs served by the
companies' own applicant-tracking systems:

- **Greenhouse**: ``https://boards-api.greenhouse.io/v1/boards/<token>/jobs``
- **Lever**:      ``https://api.lever.co/v0/postings/<token>?mode=json``
- **Ashby**:      ``https://api.ashbyhq.com/posting-api/job-board/<token>``

**2. Community feeds (secondary, attributed).** Structured ``listings.json``
files published by community-maintained internship repositories. They cover
companies whose ATSes have no public API (Workday, Taleo, in-house portals).
Every listing ingested from a feed carries a ``src:<label>`` tag and the feed
is credited in the README — see ``SOURCES.md``.

Sync rules — conservative and idempotent:

- Only titles matching the intern pattern are considered.
- Auto-ingested listings are tagged ``auto-ingested`` and are the *only*
  listings the sync will ever modify. Hand-curated files are never touched.
- Job present upstream → listing created/updated, ``last_verified`` = today.
- ATS job that vanished upstream, or feed entry flagged inactive → ``closed``.
- The same posting arriving from several sources merges into one listing
  (union of locations, earliest posted date, first source wins attribution).
- Every write goes through the Pydantic model, so bad upstream data cannot
  corrupt the repo — CI validation still gates everything.
"""

from __future__ import annotations

import hashlib
import re
from dataclasses import dataclass, field
from datetime import date, datetime, timedelta, timezone
from pathlib import Path
from typing import Any, Callable, Iterable, Optional
from urllib.parse import parse_qsl, urlencode, urlsplit, urlunsplit

import requests
import yaml

from .loader import delete_listing, load_all, write_listing
from .models import (ApplicationStatus, Category, CompanyInfo, Dates, DegreeLevel,
                     Eligibility, Internship, Location, Season, TechProfile,
                     VisaSponsorship, WorkMode, make_slug)

AUTO_TAG = "auto-ingested"
REMOTE_OPTION_TAG = "remote-available"
SOURCES_FILE = Path("automation/sources.yaml")
USER_AGENT = "InternAtlas-Sync/2.0 (+https://github.com/internatlas)"

# \bintern\b or \binternship(s)\b — avoids matching "International"/"Internal".
_INTERN_RE = re.compile(r"\bintern(ship)?s?\b", re.IGNORECASE)
_EXCLUDE_RE = re.compile(r"\binternal\b|\binternational\b", re.IGNORECASE)
_REMOTE_RE = re.compile(r"\bremote\b|\bwork from home\b|\bwfh\b", re.IGNORECASE)

_TERM_RE = re.compile(r"(Summer|Fall|Winter|Spring)\s*'?\s*(\d{2,4})", re.IGNORECASE)

# Title keywords → category overrides (first match wins), checked before the
# per-source default category.
_CATEGORY_KEYWORDS: list[tuple[re.Pattern[str], Category]] = [
    (re.compile(r"machine learning|\bml\b|deep learning", re.I), Category.MACHINE_LEARNING),
    (re.compile(r"\bai\b|artificial intelligence|\bllm\b|gen(erative)? ai", re.I), Category.AI),
    (re.compile(r"data scien", re.I), Category.DATA_SCIENCE),
    (re.compile(r"data engineer|analytics engineer", re.I), Category.DATA_ENGINEERING),
    (re.compile(r"quant|trading", re.I), Category.QUANT),
    (re.compile(r"security|infosec|appsec", re.I), Category.SECURITY),
    (re.compile(r"embedded|firmware", re.I), Category.EMBEDDED),
    (re.compile(r"hardware|asic|fpga|silicon", re.I), Category.HARDWARE),
    (re.compile(r"\bcloud\b|infrastructure|platform eng|devops|\bsre\b", re.I), Category.CLOUD),
    (re.compile(r"product manage|\bapm\b", re.I), Category.PRODUCT),
    (re.compile(r"design(er)?\b|\bux\b|\bui\b", re.I), Category.DESIGN),
    (re.compile(r"research", re.I), Category.RESEARCH),
]

# Feed `category` strings (Simplify taxonomy) → our categories. The title
# keyword pass still runs first, so e.g. an "ML Intern" filed under
# "Data Science, AI & Machine Learning" lands in machine-learning.
_FEED_CATEGORY_MAP: dict[str, Category] = {
    "software engineering": Category.SOFTWARE_ENGINEERING,
    "quantitative finance": Category.QUANT,
    "data science, ai & machine learning": Category.DATA_SCIENCE,
    "hardware engineering": Category.HARDWARE,
    "product management": Category.PRODUCT,
    "other": Category.SOFTWARE_ENGINEERING,
}

_SPONSORSHIP_MAP: dict[str, VisaSponsorship] = {
    "offers sponsorship": VisaSponsorship.YES,
    "does not offer sponsorship": VisaSponsorship.NO,
    "u.s. citizenship is required": VisaSponsorship.NO,
    "other": VisaSponsorship.UNKNOWN,
}

_DEGREE_MAP: dict[str, DegreeLevel] = {
    "master's": DegreeLevel.MASTERS,
    "masters": DegreeLevel.MASTERS,
    "phd": DegreeLevel.PHD,
    "ph.d.": DegreeLevel.PHD,
}

_US_STATES = {
    "AL", "AK", "AZ", "AR", "CA", "CO", "CT", "DE", "FL", "GA", "HI", "ID", "IL",
    "IN", "IA", "KS", "KY", "LA", "ME", "MD", "MA", "MI", "MN", "MS", "MO", "MT",
    "NE", "NV", "NH", "NJ", "NM", "NY", "NC", "ND", "OH", "OK", "OR", "PA", "RI",
    "SC", "SD", "TN", "TX", "UT", "VT", "VA", "WA", "WV", "WI", "WY", "DC",
}

_COUNTRY_ALIASES = {
    "united states": "USA", "us": "USA", "usa": "USA", "u.s.": "USA",
    "united kingdom": "UK", "uk": "UK", "england": "UK",
}

_TRACKING_PARAMS = {
    "utm_source", "utm_medium", "utm_campaign", "utm_term", "utm_content",
    "gh_src", "lever-source", "ref", "referrer", "trk", "simplify",
}


# ── configuration ─────────────────────────────────────────────────────────────

@dataclass(frozen=True)
class Source:
    """One company's ATS board, as declared in automation/sources.yaml."""
    company: str
    slug: str
    ats: str                       # "greenhouse" | "lever" | "ashby"
    token: str
    default_category: Category
    country: str = "USA"
    career_page: str | None = None


@dataclass(frozen=True)
class Feed:
    """One community feed, as declared in automation/sources.yaml."""
    label: str                     # short id used in the src:<label> tag
    name: str                      # display name for attribution
    url: str                       # raw listings.json URL
    homepage: str                  # repo/homepage to credit + link
    terms: tuple[str, ...] = ()    # allow-list of terms, e.g. ("Summer 2027",)
    season_years: dict[str, int] = field(default_factory=dict)  # fallback map


@dataclass(frozen=True)
class RawJob:
    """Normalized job record from any ATS."""
    title: str
    url: str
    location: str
    posted: date | None


def load_sources(root: Path) -> list[Source]:
    data = _read_sources_file(root)
    sources = []
    for entry in data.get("sources", []):
        sources.append(Source(
            company=entry["company"],
            slug=entry.get("slug") or make_slug(entry["company"]),
            ats=entry["ats"],
            token=entry["token"],
            default_category=Category(entry.get("default_category", "software-engineering")),
            country=entry.get("country", "USA"),
            career_page=entry.get("career_page"),
        ))
    return sources


def load_feeds(root: Path) -> list[Feed]:
    data = _read_sources_file(root)
    feeds = []
    for entry in data.get("feeds", []):
        feeds.append(Feed(
            label=entry["label"],
            name=entry.get("name", entry["label"]),
            url=entry["url"],
            homepage=entry.get("homepage", entry["url"]),
            terms=tuple(entry.get("terms", [])),
            season_years={str(k).lower(): int(v)
                          for k, v in (entry.get("season_years") or {}).items()},
        ))
    return feeds


def _read_sources_file(root: Path) -> dict[str, Any]:
    path = root / SOURCES_FILE
    if not path.exists():
        return {}
    return yaml.safe_load(path.read_text(encoding="utf-8")) or {}


# ── shared helpers ────────────────────────────────────────────────────────────

def is_internship(title: str) -> bool:
    return bool(_INTERN_RE.search(title)) and not (
        _EXCLUDE_RE.search(title) and not _INTERN_RE.search(_EXCLUDE_RE.sub("", title))
    )


def classify(title: str, default: Category) -> Category:
    for pattern, category in _CATEGORY_KEYWORDS:
        if pattern.search(title):
            return category
    return default


def clean_url(url: str) -> str:
    """Strip tracking parameters; keep everything functional (e.g. gh_jid)."""
    parts = urlsplit(url.strip())
    query = urlencode([(k, v) for k, v in parse_qsl(parts.query)
                       if k.lower() not in _TRACKING_PARAMS and not k.lower().startswith("utm")])
    return urlunsplit((parts.scheme, parts.netloc, parts.path, query, ""))


def _epoch_to_date(value: Any, today: date) -> date | None:
    """Convert a unix timestamp (s or ms) into a date; drop implausible values."""
    if not value:
        return None
    try:
        ts = float(value)
    except (TypeError, ValueError):
        return None
    if ts > 1e12:                      # milliseconds
        ts /= 1000.0
    try:
        parsed = datetime.fromtimestamp(ts, tz=timezone.utc).date()
    except (OverflowError, OSError, ValueError):
        return None
    if parsed.year < 2020 or parsed > today + timedelta(days=366):
        return None
    return parsed


def parse_location(raw: str, default_country: str) -> tuple[WorkMode, list[Location]]:
    """Parse a single ATS location string (kept for the ATS path + tests)."""
    text = raw.strip()
    if not text or _REMOTE_RE.search(text):
        return WorkMode.REMOTE, []
    parts = [p.strip() for p in re.split(r"[,;/]| - ", text) if p.strip()]
    city = parts[0] if parts else None
    state = parts[1] if len(parts) > 1 and len(parts[1]) <= 3 else None
    return WorkMode.ONSITE, [Location(city=city, state=state, country=default_country)]


def _parse_one_location(raw: str, default_country: str) -> Location | None:
    """Parse a single "City, ST" / "City, Country" / "Country" string.

    Returns ``None`` for remote-ish strings (handled by the caller).
    """
    text = raw.strip()
    if not text or _REMOTE_RE.search(text):
        return None
    parts = [p.strip() for p in text.split(",") if p.strip()]
    if not parts:
        return None
    first = parts[0]
    if len(parts) == 1:
        lowered = first.lower()
        if lowered in _COUNTRY_ALIASES:
            return Location(country=_COUNTRY_ALIASES[lowered])
        if first.upper() in _US_STATES:
            return Location(state=first.upper(), country="USA")
        return Location(city=first, country=default_country)
    second = parts[1]
    if second.upper() in _US_STATES:
        return Location(city=first, state=second.upper(), country="USA")
    country = _COUNTRY_ALIASES.get(second.lower(), second)
    return Location(city=first, country=country)


def parse_locations(raws: Iterable[str], default_country: str) -> tuple[WorkMode, list[Location], bool]:
    """Parse a list of location strings from a feed entry.

    Returns ``(work_mode, locations, remote_option)`` where *remote_option*
    is True when the posting mixes remote and physical locations.
    """
    locations: list[Location] = []
    saw_remote = False
    for raw in raws:
        loc = _parse_one_location(raw, default_country)
        if loc is None:
            saw_remote = saw_remote or bool(str(raw).strip())
            continue
        if loc not in locations:
            locations.append(loc)
    if not locations:
        return WorkMode.REMOTE, [], False
    return WorkMode.ONSITE, locations, saw_remote


def resolve_term(entry: dict[str, Any], feed: Feed) -> tuple[Season, int] | None:
    """Work out (season, year) for a feed entry, or None to skip it.

    Preference order: the entry's ``terms`` list filtered by the feed's
    allow-list, then the ``season`` key via the feed's ``season_years`` map.
    """
    terms = [t for t in entry.get("terms") or [] if isinstance(t, str)]
    candidates = [t for t in terms if not feed.terms or t in feed.terms]
    for term in candidates:
        if (m := _TERM_RE.search(term)) is not None:
            year = int(m.group(2))
            if year < 100:
                year += 2000
            return Season(m.group(1).lower()), year
    if terms:            # entry has terms but none allowed → out of scope
        return None
    season_key = str(entry.get("season") or "").lower()
    if season_key in feed.season_years:
        return Season(season_key), feed.season_years[season_key]
    return None


# ── ATS fetchers ──────────────────────────────────────────────────────────────

def _get_json(url: str, timeout: float = 25.0) -> Any:
    resp = requests.get(url, timeout=timeout, headers={"User-Agent": USER_AGENT})
    resp.raise_for_status()
    return resp.json()


def fetch_greenhouse(token: str) -> list[RawJob]:
    payload = _get_json(f"https://boards-api.greenhouse.io/v1/boards/{token}/jobs")
    today = date.today()
    jobs = []
    for j in payload.get("jobs", []):
        posted = None
        for key in ("first_published", "updated_at"):
            if j.get(key):
                try:
                    posted = datetime.fromisoformat(str(j[key]).replace("Z", "+00:00")).date()
                    break
                except ValueError:
                    pass
        jobs.append(RawJob(
            title=j.get("title", ""),
            url=j.get("absolute_url", ""),
            location=(j.get("location") or {}).get("name", ""),
            posted=posted if posted and posted <= today else posted,
        ))
    return jobs


def fetch_lever(token: str) -> list[RawJob]:
    payload = _get_json(f"https://api.lever.co/v0/postings/{token}?mode=json")
    today = date.today()
    jobs = []
    for j in payload if isinstance(payload, list) else []:
        jobs.append(RawJob(
            title=j.get("text", ""),
            url=j.get("hostedUrl", ""),
            location=(j.get("categories") or {}).get("location", "") or "",
            posted=_epoch_to_date(j.get("createdAt"), today),
        ))
    return jobs


def fetch_ashby(token: str) -> list[RawJob]:
    payload = _get_json(f"https://api.ashbyhq.com/posting-api/job-board/{token}")
    today = date.today()
    jobs = []
    for j in payload.get("jobs", []) if isinstance(payload, dict) else []:
        if j.get("isListed") is False:
            continue
        url = j.get("jobUrl") or j.get("applyUrl") or ""
        locs = [j.get("location") or ""] + [
            (s or {}).get("location", "") for s in j.get("secondaryLocations") or []
        ]
        posted = None
        for key in ("publishedAt", "publishedDate"):
            if j.get(key):
                try:
                    posted = datetime.fromisoformat(str(j[key]).replace("Z", "+00:00")).date()
                    break
                except ValueError:
                    pass
        if j.get("isRemote") and not any(_REMOTE_RE.search(x or "") for x in locs):
            locs.append("Remote")
        jobs.append(RawJob(
            title=j.get("title", ""),
            url=url,
            location="; ".join(x for x in locs if x),
            posted=posted if posted and posted <= today else posted,
        ))
    return jobs


_FETCHERS: dict[str, Callable[[str], list[RawJob]]] = {
    "greenhouse": fetch_greenhouse,
    "lever": fetch_lever,
    "ashby": fetch_ashby,
}


def fetch_feed(url: str) -> list[dict[str, Any]]:
    payload = _get_json(url, timeout=40.0)
    return payload if isinstance(payload, list) else []


# module-level hook so tests can monkeypatch feed fetching too
_FEED_FETCHER: Callable[[str], list[dict[str, Any]]] = fetch_feed


# ── mapping ───────────────────────────────────────────────────────────────────

def map_job(job: RawJob, source: Source, year: int, today: date) -> Internship | None:
    """Map one ATS job onto our model (ATS sources track Summer <year>)."""
    if not is_internship(job.title) or not job.url:
        return None
    role_slug = make_slug(job.title)[:60].rstrip("-")
    work_mode, locations = parse_location(job.location, source.country)
    return Internship(
        id=f"{source.slug}-{role_slug}-{year}",
        company=CompanyInfo(name=source.company, slug=source.slug,
                            career_page=source.career_page),  # type: ignore[arg-type]
        role=job.title.strip(),
        category=classify(job.title, source.default_category),
        apply_url=clean_url(job.url),  # type: ignore[arg-type]
        work_mode=work_mode,
        locations=locations,
        year=year,
        dates=Dates(posted=job.posted, discovered=today, last_verified=today),
        status=ApplicationStatus.OPEN,
        tech=TechProfile(),
        tags=[AUTO_TAG, "src:ats"],
    )


def map_feed_entry(entry: dict[str, Any], feed: Feed, today: date) -> Internship | None:
    """Map one community-feed entry onto our model, or None to skip it."""
    title = str(entry.get("title") or "").strip()
    url = str(entry.get("url") or "").strip()
    company_name = str(entry.get("company_name") or "").strip()
    if not title or not company_name or not url.startswith("http"):
        return None
    if not is_internship(title):
        return None
    term = resolve_term(entry, feed)
    if term is None:
        return None
    season, year = term

    slug = make_slug(company_name)
    if not slug:
        return None
    work_mode, locations, remote_option = parse_locations(entry.get("locations") or [], "USA")

    sponsorship_raw = str(entry.get("sponsorship") or "").strip().lower()
    visa = _SPONSORSHIP_MAP.get(sponsorship_raw, VisaSponsorship.UNKNOWN)
    citizenship = sponsorship_raw == "u.s. citizenship is required"
    degrees = [
        _DEGREE_MAP[d.lower()]
        for d in entry.get("degrees") or []
        if isinstance(d, str) and d.lower() in _DEGREE_MAP
    ]

    feed_category = _FEED_CATEGORY_MAP.get(str(entry.get("category") or "").strip().lower())
    category = classify(title, feed_category or Category.SOFTWARE_ENGINEERING)

    role_slug = make_slug(title)[:60].rstrip("-")
    tags = [AUTO_TAG, f"src:{feed.label}"]
    if remote_option:
        tags.append(REMOTE_OPTION_TAG)

    return Internship(
        id=f"{slug}-{role_slug}-{year}",
        company=CompanyInfo(name=company_name, slug=slug),
        role=title,
        category=category,
        apply_url=clean_url(url),  # type: ignore[arg-type]
        season=season,
        year=year,
        work_mode=work_mode,
        locations=locations,
        eligibility=Eligibility(visa_sponsorship=visa, citizenship_required=citizenship,
                                degree_levels=degrees),
        dates=Dates(posted=_epoch_to_date(entry.get("date_posted"), today),
                    discovered=today, last_verified=today),
        status=ApplicationStatus.OPEN,
        tags=tags,
    )


# ── cross-source merge ────────────────────────────────────────────────────────

def _merge(base: Internship, extra: Internship) -> None:
    """Fold *extra* (same posting from another source) into *base* in place."""
    for loc in extra.locations:
        if loc not in base.locations:
            base.locations.append(loc)
    if extra.dates.posted and (not base.dates.posted or extra.dates.posted < base.dates.posted):
        base.dates.posted = extra.dates.posted
    if base.eligibility.visa_sponsorship is VisaSponsorship.UNKNOWN:
        base.eligibility = extra.eligibility
    if REMOTE_OPTION_TAG in extra.tags and REMOTE_OPTION_TAG not in base.tags:
        base.tags.append(REMOTE_OPTION_TAG)


def reconcile_batch(candidates: list[Internship]) -> dict[str, Internship]:
    """Resolve id collisions across all sources in one sync run.

    - Same id + same (cleaned) apply URL → one listing, fields merged.
    - Same id + different URLs → genuinely different postings; every listing
      keeps a deterministic id, the lexicographically-smallest URL holds the
      base id and the others get a short URL-hash suffix. Deterministic
      regardless of fetch order, so hourly runs do not churn ids.
    """
    groups: dict[str, dict[str, Internship]] = {}
    for listing in candidates:
        url_key = clean_url(str(listing.apply_url)).lower()
        bucket = groups.setdefault(listing.id, {})
        if url_key in bucket:
            _merge(bucket[url_key], listing)
        else:
            bucket[url_key] = listing

    final: dict[str, Internship] = {}
    for base_id, bucket in groups.items():
        ordered = sorted(bucket.items())          # by url_key → deterministic
        for i, (url_key, listing) in enumerate(ordered):
            if i == 0:
                final[base_id] = listing
            else:
                suffix = hashlib.sha1(url_key.encode()).hexdigest()[:6]
                listing.id = f"{base_id}-{suffix}"
                final[listing.id] = listing

    # Second pass: collapse listings that ended up with *different* ids but the
    # *same* apply URL — e.g. the same posting listed under two spellings of a
    # company name across feeds ("Aquatic" vs "Aquatic Capital Management"), or
    # the same URL differing only by "www."/trailing slash. We key on the *same*
    # canonicalization the validator uses, so anything it would flag as a URL
    # duplicate is guaranteed to already be merged here. The lexicographically-
    # smallest id wins; the other is merged into it — deterministic across runs.
    from .dedupe import canonicalize_url

    by_url: dict[str, Internship] = {}
    for listing in sorted(final.values(), key=lambda l: l.id):
        url_key = canonicalize_url(str(listing.apply_url))
        if (winner := by_url.get(url_key)) is not None:
            _merge(winner, listing)
        else:
            by_url[url_key] = listing
    return {l.id: l for l in by_url.values()}


# ── sync ──────────────────────────────────────────────────────────────────────

def sync(root: Path | str = ".", year: int = 2027, today: date | None = None,
         only: Optional[str] = None) -> dict[str, int]:
    """Run one sync pass. Returns counters for logging.

    ``only`` restricts the pass to ``"ats"`` or ``"feeds"`` (useful for
    debugging and for environments where one class of source is unreachable).
    """
    root = Path(root)
    today = today or date.today()
    sources = load_sources(root) if only in (None, "ats") else []
    feeds = load_feeds(root) if only in (None, "feeds") else []
    counters = {"sources": len(sources), "feeds": len(feeds), "fetched": 0,
                "created": 0, "updated": 0, "closed": 0, "errors": 0}

    existing = {l.id: l for l in load_all(root).listings}
    auto_ids_by_slug: dict[str, set[str]] = {}
    for l in existing.values():
        if AUTO_TAG in l.tags:
            auto_ids_by_slug.setdefault(l.company.slug, set()).add(l.id)

    candidates: list[Internship] = []
    ats_covered_slugs: set[str] = set()
    inactive_feed_keys: set[tuple[str, str]] = set()   # (company slug, role slug)

    for source in sources:
        try:
            raw_jobs = _FETCHERS[source.ats](source.token)
        except Exception as exc:  # noqa: BLE001 — one bad source must not kill the sync
            print(f"! {source.company}: fetch failed ({exc})")
            counters["errors"] += 1
            continue
        ats_covered_slugs.add(source.slug)
        for job in sorted(raw_jobs, key=lambda j: (j.posted or date.max, j.url)):
            listing = map_job(job, source, year, today)
            if listing is not None:
                counters["fetched"] += 1
                candidates.append(listing)

    for feed in feeds:
        try:
            entries = _FEED_FETCHER(feed.url)
        except Exception as exc:  # noqa: BLE001
            print(f"! feed {feed.label}: fetch failed ({exc})")
            counters["errors"] += 1
            continue
        for entry in sorted(entries, key=lambda e: (e.get("date_posted") or 0, str(e.get("id")))):
            active = bool(entry.get("active", True)) and bool(entry.get("is_visible", True))
            if not active:
                title = str(entry.get("title") or "")
                company = str(entry.get("company_name") or "")
                if title and company:
                    inactive_feed_keys.add(
                        (make_slug(company), make_slug(title)[:60].rstrip("-")))
                continue
            listing = map_feed_entry(entry, feed, today)
            if listing is not None:
                counters["fetched"] += 1
                candidates.append(listing)

    resolved = reconcile_batch(candidates)
    seen_ids: set[str] = set()

    for listing in resolved.values():
        prior = existing.get(listing.id)
        if prior is not None and AUTO_TAG not in prior.tags:
            continue                    # never touch hand-curated listings
        seen_ids.add(listing.id)
        if prior is not None:
            # preserve first-discovered date and any enrichment humans added
            listing.dates.discovered = prior.dates.discovered
            if prior.dates.posted and not listing.dates.posted:
                listing.dates.posted = prior.dates.posted
            listing.tech = prior.tech
            listing.compensation = prior.compensation
            listing.notes = prior.notes
            if prior.eligibility != Eligibility():
                listing.eligibility = prior.eligibility
            counters["updated"] += 1
        else:
            counters["created"] += 1
        write_listing(root, listing)

    # Close stale ATS listings: the board answered, the job is gone.
    for slug in ats_covered_slugs:
        for stale_id in sorted(auto_ids_by_slug.get(slug, set()) - seen_ids):
            _close(existing[stale_id], root, today, counters)

    # Close listings the community feeds now flag inactive.
    if inactive_feed_keys:
        for l in existing.values():
            if AUTO_TAG not in l.tags or l.id in seen_ids:
                continue
            role_slug = make_slug(l.role)[:60].rstrip("-")
            if (l.company.slug, role_slug) in inactive_feed_keys:
                _close(l, root, today, counters)

    # Final safety net: remove any auto-ingested files that now share an apply
    # URL with another listing — e.g. the same posting written under a different
    # id in a previous run because a differently-spelled source came online.
    # This is what keeps `internatlas validate` (and the hourly job) green across
    # source changes; it is self-healing on every run.
    counters["pruned"] = 0
    _prune_url_collisions(root, counters, seen_ids)

    return counters


def _prune_url_collisions(root: Path, counters: dict[str, int], seen_ids: set[str]) -> None:
    """Collapse on-disk listings that share a canonical apply URL.

    Deletes only redundant *auto-ingested* copies, and only ones that are **not
    live this run** — i.e. stale orphans left over from an earlier run (say, the
    same posting written under a different id before a differently-spelled source
    came online). The listing a current source actually produces (``seen_ids``),
    or any hand-curated file, is always the one kept. Because we never delete the
    live keeper, a steady state produces no churn: once the orphan is gone it is
    never recreated. Self-healing on every run.
    """
    from .dedupe import canonicalize_url

    by_url: dict[str, list[Internship]] = {}
    for l in load_all(root).listings:
        by_url.setdefault(canonicalize_url(str(l.apply_url)), []).append(l)

    for group in by_url.values():
        if len(group) < 2:
            continue
        curated = [l for l in group if AUTO_TAG not in l.tags]
        live = [l for l in group if l.id in seen_ids]
        if curated:
            keep = min(curated, key=lambda l: l.id)          # human files always win
        elif live:
            keep = min(live, key=lambda l: l.id)             # the id a source produces now
        else:                                                # all stale orphans
            keep = min(group, key=lambda l: (l.status is not ApplicationStatus.OPEN, l.id))
        for loser in group:
            if loser is keep or AUTO_TAG not in loser.tags:  # never delete curated files
                continue
            if delete_listing(root, loser):
                counters["pruned"] += 1


def _close(listing: Internship, root: Path, today: date, counters: dict[str, int]) -> None:
    if listing.status is not ApplicationStatus.CLOSED:
        listing.status = ApplicationStatus.CLOSED
        listing.dates.last_verified = today
        write_listing(root, listing)
        counters["closed"] += 1


def main(root: str = ".", only: Optional[str] = None) -> int:
    counters = sync(root, only=only)
    print(", ".join(f"{k}={v}" for k, v in counters.items()))
    return 0

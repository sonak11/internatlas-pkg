"""Generate the top-level README.md.

The README is assembled from ``docs/README.template.md`` — human-authored prose
lives in the template; everything between generation markers is machine-owned.
"""

from __future__ import annotations

from datetime import date, datetime, timezone
from pathlib import Path

from ..models import Category, Internship, Season
from ..stats import RepoStats
from .common import GENERATED_BANNER, listing_table, sort_listings

# The term this repo is *about* — headlines the README; everything else is
# surfaced separately as "other open terms".
HEADLINE_SEASON = Season.SUMMER
HEADLINE_YEAR = 2027

SEASON_EMOJI = {
    Season.SUMMER: "☀️",
    Season.FALL: "🍂",
    Season.WINTER: "❄️",
    Season.SPRING: "🌱",
}

CATEGORY_LABELS: dict[Category, str] = {
    Category.SOFTWARE_ENGINEERING: "💻 Software Engineering",
    Category.MACHINE_LEARNING: "🧠 Machine Learning",
    Category.AI: "🤖 AI",
    Category.DATA_SCIENCE: "📊 Data Science",
    Category.DATA_ENGINEERING: "🛠 Data Engineering",
    Category.QUANT: "📈 Quant",
    Category.HARDWARE: "🔩 Hardware",
    Category.EMBEDDED: "⚙️ Embedded",
    Category.SECURITY: "🔐 Security",
    Category.CLOUD: "☁️ Cloud",
    Category.PRODUCT: "🧭 Product",
    Category.DESIGN: "🎨 Design",
    Category.RESEARCH: "🔬 Research",
    Category.FINANCE: "💰 Finance",
    Category.CONSULTING: "🤝 Consulting",
}


def _is_headline(l: Internship) -> bool:
    return l.season is HEADLINE_SEASON and l.year == HEADLINE_YEAR


try:
    from zoneinfo import ZoneInfo
    _EASTERN = ZoneInfo("America/New_York")   # handles EST/EDT automatically
except Exception:  # pragma: no cover - only if the tz database is missing
    from datetime import timedelta
    _EASTERN = timezone(timedelta(hours=-5), "EST")


def _now_eastern() -> datetime:
    return datetime.now(_EASTERN)


def _sync_stamp() -> str:
    # e.g. "2026-07-17 3:38 PM EDT" — %Z resolves to EDT (summer) or EST (winter).
    return _now_eastern().strftime("%Y-%m-%d %-I:%M %p %Z")


def _badges(stats: RepoStats, listings: list[Internship]) -> str:
    def badge(label: str, value: str, color: str) -> str:
        label_e = label.replace(" ", "%20").replace("-", "--")
        value_e = str(value).replace(" ", "%20").replace("-", "--")
        return f"![{label}](https://img.shields.io/badge/{label_e}-{value_e}-{color}?style=for-the-badge)"

    headline = [l for l in listings if _is_headline(l)]
    headline_open = sum(1 for l in headline if l.is_open)
    updated = _now_eastern().strftime("%Y-%m-%d %-I:%M %p %Z")
    return " ".join([
        badge(f"summer {HEADLINE_YEAR}", str(len(headline)), "blue"),
        badge("open now", str(headline_open or stats.open), "brightgreen"),
        badge("companies", str(len(stats.by_company)), "purple"),
        badge("visa sponsors", str(len(stats.sponsoring_companies)), "orange"),
        badge("remote roles", str(stats.remote_count), "teal"),
        badge("sync", "hourly", "success"),
        badge("updated", updated, "lightgrey"),
    ])


def _quick_stats(stats: RepoStats, listings: list[Internship]) -> str:
    headline = [l for l in listings if _is_headline(l)]
    headline_open = sum(1 for l in headline if l.is_open)
    offseason = [l for l in listings if not _is_headline(l)]
    offseason_open = sum(1 for l in offseason if l.is_open)

    lines = ["| Metric | Value |", "|---|---|"]
    lines.append(f"| ☀️ Summer {HEADLINE_YEAR} listings | **{len(headline)}** |")
    lines.append(f"| Summer {HEADLINE_YEAR} open now | **{headline_open}** |")
    if offseason:
        lines.append(f"| Other open terms (Fall/Winter/Spring) | **{offseason_open}** |")
    lines.append(f"| Companies | **{len(stats.by_company)}** |")
    if stats.salary.average is not None:
        lines.append(f"| Average hourly (reported) | **${stats.salary.average:.2f}** |")
    if stats.top_languages:
        top3 = ", ".join(name for name, _ in stats.top_languages[:3])
        lines.append(f"| Most requested languages | {top3} |")
    lines.append(f"| Last synced | **{_sync_stamp()}** |")
    return "\n".join(lines)


def _category_nav() -> str:
    cells = [
        f"[{label}](generated/categories/{cat.value}.md)"
        for cat, label in CATEGORY_LABELS.items()
    ]
    rows = ["| " + " | ".join(cells[i:i + 5]) + " |" for i in range(0, len(cells), 5)]
    sep = "|" + "---|" * min(5, len(cells))
    return "\n".join([rows[0], sep, *rows[1:]])


def _deadline_alert(stats: RepoStats) -> str:
    if not stats.deadlines_this_week:
        return ""
    lines = ["> [!WARNING]", "> **⏰ Deadlines in the next 7 days:**"]
    for l in stats.deadlines_this_week[:10]:
        lines.append(f"> - **{l.company.name}** — [{l.role}]({l.apply_url}) closes **{l.dates.deadline}**")
    return "\n".join(lines) + "\n"


def _posted_key(l: Internship):
    """Newest first: sort by posting date (fall back to discovered), desc."""
    return l.dates.posted or l.dates.discovered


def _sort_newest(listings: list[Internship]) -> list[Internship]:
    """Open roles first; within that, newest-posted first, then company name."""
    return sorted(
        listings,
        key=lambda l: (l.is_open is False, _reverse_date(l), l.company.name.lower()),
    )


def _reverse_date(l: Internship) -> str:
    """A key string that sorts *descending* by date in an ascending sort."""
    d = _posted_key(l)
    return f"{9999 - d.year:04d}-{12 - d.month:02d}-{31 - d.day:02d}"


def _listings_by_category(listings: list[Internship], newest_first: bool = True) -> str:
    """Listings inline, grouped by category — no clicking around.

    Within each category, open roles come first and the newest postings lead,
    so the freshest opportunities are always at the top.
    """
    blocks: list[str] = []
    for cat, label in CATEGORY_LABELS.items():
        subset = [l for l in listings if l.category is cat]
        if not subset:
            continue
        subset = _sort_newest(subset) if newest_first else sort_listings(subset)
        open_n = sum(1 for l in subset if l.is_open)
        blocks.append(
            f"### {label} ({len(subset)}{f' · {open_n} open' if open_n else ''})\n\n"
            f"{listing_table(subset)}\n"
        )
    return "\n".join(blocks)


def _offseason_listings(listings: list[Internship]) -> str:
    """Live postings for terms other than the headline one (Fall/Winter/Spring),
    grouped and clearly labeled so nobody mistakes them for Summer 2027."""
    others = [l for l in listings if not _is_headline(l)]
    if not others:
        return "_No other terms currently listed._"

    # Order: by year, then season within the academic cycle.
    season_order = {Season.FALL: 0, Season.WINTER: 1, Season.SPRING: 2, Season.SUMMER: 3}
    groups: dict[tuple[int, Season], list[Internship]] = {}
    for l in others:
        groups.setdefault((l.year, l.season), []).append(l)

    blocks: list[str] = []
    for (year, season) in sorted(groups, key=lambda k: (k[0], season_order.get(k[1], 9))):
        subset = _sort_newest(groups[(year, season)])
        open_n = sum(1 for l in subset if l.is_open)
        emoji = SEASON_EMOJI.get(season, "•")
        title = f"{season.value.title()} {year}"
        blocks.append(
            f"### {emoji} {title} ({len(subset)}{f' · {open_n} open' if open_n else ''})\n\n"
            f"{listing_table(subset)}\n"
        )
    return "\n".join(blocks)


def _data_sources(root: Path | None, listings: list[Internship]) -> str:
    """Attribution block, built from each listing's ``src:<label>`` tag.

    Counts are live, and every community feed we draw from is credited with a
    link. Provenance detail lives in SOURCES.md.
    """
    from collections import Counter

    counts: Counter = Counter()
    for l in listings:
        label = next((t.split(":", 1)[1] for t in l.tags if t.startswith("src:")), None)
        if label:
            counts[label] += 1

    feed_meta: dict[str, tuple[str, str]] = {}
    if root is not None:
        try:
            from ..ingest import load_feeds
            for f in load_feeds(root):
                feed_meta[f.label] = (f.name, f.homepage)
        except Exception:  # noqa: BLE001 — attribution must never break generation
            pass

    if not counts:
        return ""

    rows = ["| Source | Listings | Type |", "|---|---:|---|"]
    if counts.get("ats"):
        rows.append(f"| Company ATS boards (Greenhouse · Lever · Ashby) | {counts['ats']} | Direct API |")
    for label, n in sorted(counts.items(), key=lambda kv: (-kv[1], kv[0])):
        if label == "ats":
            continue
        name, home = feed_meta.get(label, (label, ""))
        display = f"[{name}]({home})" if home else name
        rows.append(f"| {display} | {n} | Community feed |")
    return "\n".join(rows)


def render(listings: list[Internship], stats: RepoStats, template_path: Path,
           root: Path | None = None) -> str:
    template = template_path.read_text(encoding="utf-8")
    ordered = sort_listings(listings)
    headline = [l for l in listings if _is_headline(l)]

    sections: dict[str, str] = {
        "BADGES": _badges(stats, listings),
        "QUICK_STATS": _quick_stats(stats, listings),
        "CATEGORY_NAV": _category_nav(),
        "DEADLINE_ALERT": _deadline_alert(stats),
        # Headline section = Summer 2027 only, newest first.
        "LISTINGS_BY_CATEGORY": _listings_by_category(headline, newest_first=True),
        "LISTING_COUNT": str(len(headline)),
        # New sections.
        "OFFSEASON_LISTINGS": _offseason_listings(listings),
        "OFFSEASON_COUNT": str(len(listings) - len(headline)),
        "DATA_SOURCES": _data_sources(root, listings),
        "LAST_SYNC": _sync_stamp(),
        "TOTAL_COUNT": str(stats.total),
        # Back-compat: older templates may still reference these.
        "ALL_LISTINGS": listing_table(ordered),
    }
    out = template
    for key, value in sections.items():
        out = out.replace(f"{{{{{key}}}}}", value)
    return GENERATED_BANNER + out


def write(root: Path, listings: list[Internship], stats: RepoStats) -> Path:
    template = root / "docs" / "README.template.md"
    target = root / "README.md"
    target.write_text(render(listings, stats, template, root=root), encoding="utf-8")
    return target

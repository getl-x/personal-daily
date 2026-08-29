from __future__ import annotations

import hashlib
import html
import json
import re
from collections import defaultdict
from datetime import date, datetime, timedelta, timezone
from pathlib import Path
from typing import Any
from zoneinfo import ZoneInfo

import feedparser
import requests


ROOT = Path(__file__).resolve().parent
CONFIG_PATH = ROOT / "config.json"
DATA_PATH = ROOT / "data" / "items.json"
SITE_PATH = ROOT / "site"
ARCHIVE_PATH = SITE_PATH / "archive"

TAG_PATTERN = re.compile(r"<[^>]+>")
SPACE_PATTERN = re.compile(r"\s+")

CATEGORY_COLORS = {
    "科技": "#2563eb",
    "GitHub 热门项目": "#7c3aed",
    "游戏": "#db2777",
    "时政热点": "#ea580c",
}


def load_json(path: Path, default: Any) -> Any:
    if not path.exists():
        return default

    try:
        return json.loads(path.read_text(encoding="utf-8"))
    except (json.JSONDecodeError, OSError):
        return default


def save_json(path: Path, value: Any) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(
        json.dumps(value, ensure_ascii=False, indent=2) + "\n",
        encoding="utf-8",
    )


def clean_text(value: Any, limit: int = 420) -> str:
    if not value:
        return ""

    text = TAG_PATTERN.sub(" ", str(value))
    text = html.unescape(text)
    text = SPACE_PATTERN.sub(" ", text).strip()

    if len(text) > limit:
        return text[:limit].rstrip() + "……"

    return text


def parse_feed_time(entry: Any) -> datetime:
    for field in ("published_parsed", "updated_parsed", "created_parsed"):
        value = entry.get(field)

        if value:
            return datetime(
                value.tm_year,
                value.tm_mon,
                value.tm_mday,
                value.tm_hour,
                value.tm_min,
                value.tm_sec,
                tzinfo=timezone.utc,
            )

    return datetime.now(timezone.utc)


def parse_datetime(value: Any) -> datetime:
    try:
        parsed = datetime.fromisoformat(str(value))
    except (TypeError, ValueError):
        return datetime.now(timezone.utc)

    if parsed.tzinfo is None:
        parsed = parsed.replace(tzinfo=timezone.utc)

    return parsed


def make_key(link: str, title: str, source: str) -> str:
    raw = link or f"{source}:{title}"
    return hashlib.sha256(raw.encode("utf-8")).hexdigest()


def entry_summary(entry: Any) -> str:
    summary = entry.get("summary") or entry.get("description")

    if not summary:
        content = entry.get("content") or []
        if content and isinstance(content[0], dict):
            summary = content[0].get("value", "")

    return clean_text(summary)


def fetch_feed(
    session: requests.Session,
    source: dict[str, Any],
    cutoff: datetime,
    default_max_items: int,
) -> list[dict[str, Any]]:
    response = session.get(source["url"], timeout=(10, 35))
    response.raise_for_status()

    parsed = feedparser.parse(response.content)

    if parsed.bozo and not parsed.entries:
        raise RuntimeError(str(parsed.bozo_exception))

    max_items = int(source.get("max_items", default_max_items))
    items: list[dict[str, Any]] = []

    for entry in parsed.entries:
        published = parse_feed_time(entry)

        if published < cutoff:
            continue

        title = clean_text(entry.get("title"), limit=200)
        link = str(entry.get("link") or entry.get("id") or "").strip()

        if not title:
            continue

        items.append(
            {
                "key": make_key(link, title, source["name"]),
                "title": title,
                "link": link,
                "summary": entry_summary(entry),
                "source": source["name"],
                "category": source.get("category", "未分类"),
                "published": published.isoformat(),
            }
        )

        if len(items) >= max_items:
            break

    return items


def esc(value: Any) -> str:
    return html.escape(str(value), quote=True)


def tidy_html(value: str) -> str:
    """Remove indentation-only trailing spaces from generated artifacts."""
    return "\n".join(line.rstrip() for line in value.splitlines()) + "\n"


def category_navigation(items: list[dict[str, Any]]) -> str:
    counts: dict[str, int] = defaultdict(int)

    for item in items:
        counts[item.get("category", "未分类")] += 1

    if not counts:
        return ""

    links = []
    for category in sorted(counts):
        anchor = hashlib.md5(category.encode("utf-8")).hexdigest()[:10]
        links.append(
            f'<a class="category-pill" href="#{anchor}">'
            f"{esc(category)} <strong>{counts[category]}</strong></a>"
        )

    return f'<nav class="category-nav">{"".join(links)}</nav>'


def render_daily_page(
    *,
    title: str,
    subtitle: str,
    local_date: str,
    items: list[dict[str, Any]],
    errors: list[str],
    local_zone: ZoneInfo,
    archive_link: str,
    home_link: str,
    generated_at: str,
) -> str:
    grouped: dict[str, list[dict[str, Any]]] = defaultdict(list)

    for item in items:
        grouped[item.get("category", "未分类")].append(item)

    sections = []

    for category in sorted(grouped):
        anchor = hashlib.md5(category.encode("utf-8")).hexdigest()[:10]
        color = CATEGORY_COLORS.get(category, "#475569")
        cards = []

        for item in sorted(
            grouped[category],
            key=lambda value: value.get("published", ""),
            reverse=True,
        ):
            published = parse_datetime(item.get("published"))
            local_time = published.astimezone(local_zone).strftime("%m-%d %H:%M")

            item_title = esc(item.get("title", "无标题"))
            source = esc(item.get("source", "未知来源"))
            link = esc(item.get("link", ""))
            summary = esc(item.get("summary", ""))

            if link:
                heading = (
                    f'<h3><a href="{link}" target="_blank" '
                    f'rel="noopener noreferrer">{item_title}</a></h3>'
                )
            else:
                heading = f"<h3>{item_title}</h3>"

            summary_html = f'<p class="summary">{summary}</p>' if summary else ""

            cards.append(
                f"""
                <article class="card">
                    {heading}
                    {summary_html}
                    <div class="meta">
                        <span>{source}</span>
                        <span>{esc(local_time)}</span>
                    </div>
                </article>
                """
            )

        sections.append(
            f"""
            <section id="{anchor}" style="--category-color: {color}">
                <div class="section-heading">
                    <h2>{esc(category)}</h2>
                    <span>{len(cards)} 条</span>
                </div>
                {''.join(cards)}
            </section>
            """
        )

    if not sections:
        sections.append(
            """
            <div class="empty">
                <strong>今天暂时没有发现新内容。</strong>
                <p>可能是信息源最近没有更新，也可以稍后手动重新运行。</p>
            </div>
            """
        )

    error_html = ""
    if errors:
        error_items = "".join(f"<li>{esc(error)}</li>" for error in errors)
        error_html = f"""
        <details class="errors">
            <summary>{len(errors)} 个信息源读取失败</summary>
            <ul>{error_items}</ul>
        </details>
        """

    nav = category_navigation(items)

    return f"""<!doctype html>
<html lang="zh-CN">
<head>
    <meta charset="utf-8">
    <meta name="viewport" content="width=device-width, initial-scale=1">
    <meta name="color-scheme" content="light dark">
    <title>{esc(title)} · {esc(local_date)}</title>
    <style>
        :root {{
            color-scheme: light dark;
            --background: #f4f6fa;
            --surface: rgba(255, 255, 255, 0.92);
            --surface-strong: #ffffff;
            --text: #182033;
            --muted: #667085;
            --border: #e1e6ef;
            --accent: #2563eb;
            --shadow: 0 10px 30px rgba(31, 41, 55, 0.07);
        }}

        @media (prefers-color-scheme: dark) {{
            :root {{
                --background: #0b0f17;
                --surface: rgba(20, 27, 39, 0.92);
                --surface-strong: #141b27;
                --text: #edf2f7;
                --muted: #9ca8ba;
                --border: #2a3444;
                --accent: #60a5fa;
                --shadow: 0 12px 35px rgba(0, 0, 0, 0.22);
            }}
        }}

        * {{ box-sizing: border-box; }}

        html {{ scroll-behavior: smooth; }}

        body {{
            margin: 0;
            background:
                radial-gradient(circle at top left, rgba(37, 99, 235, 0.10), transparent 32rem),
                var(--background);
            color: var(--text);
            font-family: Inter, ui-sans-serif, system-ui, -apple-system,
                BlinkMacSystemFont, "Segoe UI", sans-serif;
            line-height: 1.68;
        }}

        main {{
            width: min(920px, calc(100% - 28px));
            margin: 0 auto;
            padding: 54px 0 80px;
        }}

        .hero {{
            padding: 30px;
            border: 1px solid var(--border);
            border-radius: 24px;
            background: var(--surface);
            box-shadow: var(--shadow);
            backdrop-filter: blur(12px);
        }}

        h1 {{
            margin: 0;
            font-size: clamp(30px, 6vw, 48px);
            letter-spacing: -0.04em;
            line-height: 1.12;
        }}

        .subtitle {{
            margin: 10px 0 0;
            color: var(--muted);
            font-size: 16px;
        }}

        .date-row {{
            display: flex;
            flex-wrap: wrap;
            align-items: center;
            justify-content: space-between;
            gap: 12px;
            margin-top: 24px;
            color: var(--muted);
            font-size: 14px;
        }}

        .top-links {{ display: flex; gap: 16px; }}

        a {{ color: var(--accent); text-decoration: none; }}
        a:hover {{ text-decoration: underline; }}

        .category-nav {{
            display: flex;
            flex-wrap: wrap;
            gap: 10px;
            margin: 22px 0 2px;
        }}

        .category-pill {{
            padding: 7px 12px;
            border: 1px solid var(--border);
            border-radius: 999px;
            background: var(--surface-strong);
            color: var(--text);
            font-size: 14px;
        }}

        .category-pill strong {{ color: var(--muted); margin-left: 4px; }}

        section {{ scroll-margin-top: 20px; margin-top: 42px; }}

        .section-heading {{
            display: flex;
            align-items: baseline;
            justify-content: space-between;
            gap: 18px;
            padding-bottom: 10px;
            border-bottom: 2px solid var(--category-color);
        }}

        .section-heading h2 {{ margin: 0; font-size: 23px; }}
        .section-heading span {{ color: var(--muted); font-size: 13px; }}

        .card {{
            margin: 14px 0;
            padding: 21px 22px;
            border: 1px solid var(--border);
            border-radius: 16px;
            background: var(--surface);
            box-shadow: var(--shadow);
        }}

        .card h3 {{ margin: 0; font-size: 18px; line-height: 1.5; }}
        .summary {{ margin: 10px 0; color: var(--text); }}

        .meta {{
            display: flex;
            flex-wrap: wrap;
            justify-content: space-between;
            gap: 8px 18px;
            margin-top: 12px;
            color: var(--muted);
            font-size: 13px;
        }}

        .empty, .errors {{
            margin-top: 24px;
            padding: 20px;
            border: 1px solid var(--border);
            border-radius: 16px;
            background: var(--surface);
        }}

        .empty p {{ margin-bottom: 0; color: var(--muted); }}
        .errors summary {{ cursor: pointer; }}
        .errors li {{ margin: 6px 0; overflow-wrap: anywhere; }}

        footer {{
            margin-top: 52px;
            text-align: center;
            color: var(--muted);
            font-size: 13px;
        }}

        @media (max-width: 600px) {{
            main {{ width: min(100% - 20px, 920px); padding-top: 18px; }}
            .hero {{ padding: 22px 18px; border-radius: 18px; }}
            .card {{ padding: 18px; }}
            .summary {{ font-size: 15px; }}
        }}
    </style>
</head>
<body>
    <main>
        <header class="hero">
            <h1>{esc(title)}</h1>
            <p class="subtitle">{esc(subtitle)}</p>
            <div class="date-row">
                <span>{esc(local_date)} · 共 {len(items)} 条</span>
                <nav class="top-links">
                    <a href="{esc(home_link)}">今天</a>
                    <a href="{esc(archive_link)}">历史归档</a>
                </nav>
            </div>
            {nav}
        </header>

        {error_html}
        {''.join(sections)}

        <footer>
            最后生成：{esc(generated_at)} · GitHub Actions 自动更新
        </footer>
    </main>
</body>
</html>
"""


def render_archive_index(title: str, dates: list[str]) -> str:
    links = "\n".join(
        f'<li><a href="{esc(value)}.html">{esc(value)}</a></li>'
        for value in dates
    )
    if not links:
        links = "<li>暂时没有归档</li>"

    return f"""<!doctype html>
<html lang="zh-CN">
<head>
    <meta charset="utf-8">
    <meta name="viewport" content="width=device-width, initial-scale=1">
    <meta name="color-scheme" content="light dark">
    <title>历史归档 · {esc(title)}</title>
    <style>
        :root {{ color-scheme: light dark; }}
        body {{
            width: min(720px, calc(100% - 32px));
            margin: 48px auto;
            font-family: ui-sans-serif, system-ui, -apple-system, "Segoe UI", sans-serif;
            line-height: 1.8;
        }}
        a {{ color: #3b82f6; text-decoration: none; }}
        a:hover {{ text-decoration: underline; }}
        li {{ margin: 8px 0; }}
    </style>
</head>
<body>
    <p><a href="../index.html">← 返回今天</a></p>
    <h1>历史归档</h1>
    <ul>{links}</ul>
</body>
</html>
"""


def main() -> None:
    config = load_json(CONFIG_PATH, {})
    history = load_json(DATA_PATH, [])

    title = config.get("title", "我的个人信息日报")
    subtitle = config.get("subtitle", "每日自动更新")
    timezone_name = config.get("timezone", "Asia/Shanghai")
    lookback_hours = int(config.get("lookback_hours", 48))
    retention_days = int(config.get("retention_days", 180))
    max_items_per_feed = int(config.get("max_items_per_feed", 5))
    max_daily_items = int(config.get("max_daily_items", 80))
    feeds = config.get("feeds", [])

    local_zone = ZoneInfo(timezone_name)
    now_utc = datetime.now(timezone.utc)
    now_local = now_utc.astimezone(local_zone)
    today = now_local.date().isoformat()
    cutoff = now_utc - timedelta(hours=lookback_hours)

    session = requests.Session()
    session.headers.update(
        {
            "User-Agent": (
                "personal-daily/1.0 "
                "(+https://github.com; personal RSS reader)"
            ),
            "Accept": "application/rss+xml, application/atom+xml, "
            "application/xml, text/xml;q=0.9, */*;q=0.5",
        }
    )

    configured_sources = {
        str(source.get("name", ""))
        for source in feeds
        if source.get("name")
    }

    existing = {
        item["key"]: item
        for item in history
        if (
            isinstance(item, dict)
            and item.get("key")
            and item.get("source") in configured_sources
        )
    }

    errors: list[str] = []
    fetched_count = 0

    for source in feeds:
        try:
            fetched = fetch_feed(
                session=session,
                source=source,
                cutoff=cutoff,
                default_max_items=max_items_per_feed,
            )
            fetched_count += len(fetched)

            for item in fetched:
                previous = existing.get(item["key"])

                if previous:
                    first_seen = previous.get("first_seen", today)
                    previous.update(item)
                    previous["first_seen"] = first_seen
                else:
                    item["first_seen"] = today
                    existing[item["key"]] = item

            print(f"OK   {source['name']}: {len(fetched)} item(s)")
        except Exception as error:  # Continue when one external feed is down.
            source_name = source.get("name", "未知信息源")
            message = f"{source_name}: {type(error).__name__}: {error}"
            errors.append(message)
            print(f"WARN {message}")

    retention_start = date.fromisoformat(today) - timedelta(days=retention_days)
    retained: list[dict[str, Any]] = []

    for item in existing.values():
        try:
            first_seen = date.fromisoformat(item["first_seen"])
        except (KeyError, TypeError, ValueError):
            first_seen = date.fromisoformat(today)
            item["first_seen"] = today

        if first_seen >= retention_start:
            retained.append(item)

    retained.sort(
        key=lambda item: item.get("published", ""),
        reverse=True,
    )

    today_items = [
        item for item in retained if item.get("first_seen") == today
    ][:max_daily_items]

    generated_at = now_local.strftime("%Y-%m-%d %H:%M %Z")

    SITE_PATH.mkdir(parents=True, exist_ok=True)
    ARCHIVE_PATH.mkdir(parents=True, exist_ok=True)

    index_page = render_daily_page(
        title=title,
        subtitle=subtitle,
        local_date=today,
        items=today_items,
        errors=errors,
        local_zone=local_zone,
        archive_link="archive/index.html",
        home_link="index.html",
        generated_at=generated_at,
    )

    archive_page = render_daily_page(
        title=title,
        subtitle=subtitle,
        local_date=today,
        items=today_items,
        errors=errors,
        local_zone=local_zone,
        archive_link="index.html",
        home_link="../index.html",
        generated_at=generated_at,
    )

    archive_dates = sorted(
        {
            item.get("first_seen")
            for item in retained
            if item.get("first_seen")
        },
        reverse=True,
    )

    (SITE_PATH / "index.html").write_text(
        tidy_html(index_page),
        encoding="utf-8",
    )
    (ARCHIVE_PATH / f"{today}.html").write_text(
        tidy_html(archive_page),
        encoding="utf-8",
    )
    (ARCHIVE_PATH / "index.html").write_text(
        tidy_html(render_archive_index(title, archive_dates)),
        encoding="utf-8",
    )

    save_json(DATA_PATH, retained)

    print()
    print(f"Date: {today}")
    print(f"Feeds configured: {len(feeds)}")
    print(f"Items fetched in window: {fetched_count}")
    print(f"Items shown today: {len(today_items)}")
    print(f"Feed errors: {len(errors)}")


if __name__ == "__main__":
    main()

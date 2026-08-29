from __future__ import annotations

import hashlib
import html
import json
import re
from collections import defaultdict
from datetime import date, datetime, timedelta, timezone
from pathlib import Path
from typing import Any
from urllib.parse import urljoin
from zoneinfo import ZoneInfo

import feedparser
import requests
from bs4 import BeautifulSoup


ROOT = Path(__file__).resolve().parent
SITE_PATH = ROOT / "site"
DOMESTIC_CONFIG_PATH = ROOT / "config.cn.json"
GLOBAL_CONFIG_PATH = ROOT / "config.global.json"
DOMESTIC_DATA_PATH = ROOT / "data" / "items-cn.json"
GLOBAL_DATA_PATH = ROOT / "data" / "items-global.json"
LEGACY_GLOBAL_DATA_PATH = ROOT / "data" / "items.json"
GLOBAL_SITE_PATH = SITE_PATH / "view" / "2"

TAG_PATTERN = re.compile(r"<[^>]+>")
SPACE_PATTERN = re.compile(r"\s+")

CATEGORY_COLORS = {
    "今日精选": "#059669",
    "科技": "#2563eb",
    "GitHub 热门项目": "#7c3aed",
    "财经商业": "#0f766e",
    "科学探索": "#0891b2",
    "游戏": "#db2777",
    "国内要闻": "#ea580c",
    "时政热点": "#ea580c",
}

DATE_PATTERNS = (
    re.compile(r"/(20\d{2})-(\d{2})-(\d{2})/"),
    re.compile(r"/(20\d{2})/(\d{2})/(\d{2})/"),
    re.compile(r"(?:t|/)(20\d{2})(\d{2})(\d{2})(?:_|/|\.)"),
)

FEATURED_POSITIVE_KEYWORDS = (
    "政策", "法规", "发布", "突破", "研究", "发现", "灾害", "救援",
    "经济", "法院", "安全", "航天", "芯片", "人工智能", "机器人",
    "government", "policy", "economy", "court", "research", "study",
    "discovery", "launch", "space", "climate", "security", "election",
    "artificial intelligence", "robot",
)

FEATURED_NEGATIVE_KEYWORDS = (
    "抽奖", "礼包", "充值", "折扣", "促销", "美女", "性感", "玉足",
    "造型", "预购", "偷窃", "被盗", "网红", "celebrity", "giveaway",
    "discount", "sale", "preorder", "stolen", "beer", "trailer reaction",
)

FEATURED_PRIORITY_SOURCES = (
    "中国科学院", "科学网", "新华网", "人民网", "中国新闻网", "央广网",
    "联合国", "NASA", "Nature", "BBC", "MIT Technology Review",
)


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


def source_accepts_title(source: dict[str, Any], title: str) -> bool:
    title_lower = title.casefold()
    include_keywords = [
        str(value).casefold()
        for value in source.get("include_keywords", [])
        if value
    ]
    exclude_keywords = [
        str(value).casefold()
        for value in source.get("exclude_keywords", [])
        if value
    ]

    if include_keywords and not any(
        keyword in title_lower
        for keyword in include_keywords
    ):
        return False

    return not any(keyword in title_lower for keyword in exclude_keywords)


def infer_datetime(value: str, fallback: datetime) -> datetime:
    normalized = clean_text(value, limit=500)

    for pattern in DATE_PATTERNS:
        match = pattern.search(normalized)
        if not match:
            continue

        try:
            return datetime(
                int(match.group(1)),
                int(match.group(2)),
                int(match.group(3)),
                tzinfo=timezone.utc,
            )
        except ValueError:
            continue

    for date_format in (
        "%Y-%m-%d %H:%M:%S",
        "%Y-%m-%d %H:%M",
        "%Y-%m-%d",
        "%Y年%m月%d日 %H:%M",
        "%Y年%m月%d日",
    ):
        match = re.search(
            r"20\d{2}(?:-|年)\d{1,2}(?:-|月)\d{1,2}(?:日)?"
            r"(?:\s+\d{1,2}:\d{2}(?::\d{2})?)?",
            normalized,
        )
        if not match:
            continue

        try:
            return datetime.strptime(match.group(0), date_format).replace(
                tzinfo=timezone.utc,
            )
        except ValueError:
            continue

    return fallback


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

        if not title or not source_accepts_title(source, title):
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


def fetch_html_listing(
    session: requests.Session,
    source: dict[str, Any],
    cutoff: datetime,
    default_max_items: int,
) -> list[dict[str, Any]]:
    response = session.get(source["url"], timeout=(10, 35))
    response.raise_for_status()

    if source.get("encoding"):
        response.encoding = str(source["encoding"])
    elif not response.encoding or response.encoding.casefold() == "iso-8859-1":
        response.encoding = response.apparent_encoding

    soup = BeautifulSoup(response.text, "html.parser")
    item_selector = source.get("item_selector", "a[href]")
    link_selector = source.get("link_selector")
    title_selector = source.get("title_selector")
    summary_selector = source.get("summary_selector")
    date_selector = source.get("date_selector")
    title_attribute = source.get("title_attribute")
    link_pattern = re.compile(source["link_pattern"]) if source.get("link_pattern") else None
    max_items = int(source.get("max_items", default_max_items))
    items: list[dict[str, Any]] = []
    seen_links: set[str] = set()
    fallback_time = datetime.now(timezone.utc)

    for container in soup.select(str(item_selector)):
        link_node = container.select_one(str(link_selector)) if link_selector else container

        if not getattr(link_node, "get", None):
            continue

        raw_link = str(link_node.get("href") or "").strip()
        link = urljoin(response.url, raw_link)

        if not raw_link or link in seen_links:
            continue
        if link_pattern and not link_pattern.search(link):
            continue

        title_node = container.select_one(str(title_selector)) if title_selector else container

        if title_attribute:
            title = clean_text(link_node.get(str(title_attribute)), limit=200)
        else:
            title = clean_text(
                title_node.get_text(" ", strip=True) if title_node else "",
                limit=200,
            )

        if not title or not source_accepts_title(source, title):
            continue

        summary = ""
        if summary_selector:
            summary_node = container.select_one(str(summary_selector))
            if summary_node:
                summary = clean_text(summary_node.get_text(" ", strip=True))

        date_text = link
        if date_selector:
            date_node = container.select_one(str(date_selector))
            if date_node:
                date_text += " " + date_node.get_text(" ", strip=True)

        published = infer_datetime(date_text, fallback_time)
        if published < cutoff:
            continue

        seen_links.add(link)
        items.append(
            {
                "key": make_key(link, title, source["name"]),
                "title": title,
                "link": link,
                "summary": summary,
                "source": source["name"],
                "category": source.get("category", "未分类"),
                "published": published.isoformat(),
            }
        )

        if len(items) >= max_items:
            break

    if not items:
        raise RuntimeError("页面中没有找到符合规则的最新条目")

    return items


def fetch_source(
    session: requests.Session,
    source: dict[str, Any],
    cutoff: datetime,
    default_max_items: int,
) -> list[dict[str, Any]]:
    if source.get("type", "feed") == "html":
        return fetch_html_listing(
            session=session,
            source=source,
            cutoff=cutoff,
            default_max_items=default_max_items,
        )

    return fetch_feed(
        session=session,
        source=source,
        cutoff=cutoff,
        default_max_items=default_max_items,
    )


def normalized_title(value: str) -> str:
    return re.sub(r"[^0-9a-z\u4e00-\u9fff]+", "", value.casefold())


def select_daily_items(
    retained: list[dict[str, Any]],
    today: str,
    category_order: list[str],
    max_daily_items: int,
    max_items_per_category: int,
) -> list[dict[str, Any]]:
    grouped: dict[str, list[dict[str, Any]]] = defaultdict(list)
    seen_titles: set[str] = set()

    for item in sorted(
        (
            value
            for value in retained
            if value.get("first_seen") == today
        ),
        key=lambda value: value.get("published", ""),
        reverse=True,
    ):
        title_key = normalized_title(str(item.get("title", "")))
        if title_key and title_key in seen_titles:
            continue
        if title_key:
            seen_titles.add(title_key)

        category = str(item.get("category", "未分类"))
        if len(grouped[category]) < max_items_per_category:
            grouped[category].append(item)

    selected: list[dict[str, Any]] = []
    categories = ordered_category_names(
        {category: len(values) for category, values in grouped.items()},
        category_order,
    )

    for category in categories:
        selected.extend(grouped[category])

    return selected[:max_daily_items]


def add_featured_items(
    items: list[dict[str, Any]],
    category_order: list[str],
    max_featured_items: int,
) -> list[dict[str, Any]]:
    if not items or max_featured_items <= 0:
        return items

    grouped: dict[str, list[dict[str, Any]]] = defaultdict(list)
    for item in items:
        category = str(item.get("category", "未分类"))
        if category != "今日精选":
            grouped[category].append(item)

    def featured_score(item: dict[str, Any]) -> tuple[int, str]:
        searchable = (
            f"{item.get('title', '')} {item.get('summary', '')}"
        ).casefold()
        score = sum(
            3
            for keyword in FEATURED_POSITIVE_KEYWORDS
            if keyword.casefold() in searchable
        )
        score -= sum(
            7
            for keyword in FEATURED_NEGATIVE_KEYWORDS
            if keyword.casefold() in searchable
        )
        source = str(item.get("source", ""))
        if any(value in source for value in FEATURED_PRIORITY_SOURCES):
            score += 2
        return score, str(item.get("published", ""))

    for values in grouped.values():
        values.sort(
            key=featured_score,
            reverse=True,
        )

    categories = [
        category
        for category in category_order
        if category != "今日精选" and grouped.get(category)
    ]
    categories.extend(
        category
        for category in sorted(grouped)
        if category not in categories
    )

    featured: list[dict[str, Any]] = []
    source_counts: dict[str, int] = defaultdict(int)
    cursor = 0

    while len(featured) < max_featured_items and categories:
        category = categories[cursor % len(categories)]
        candidates = grouped[category]
        chosen_index = next(
            (
                index
                for index, candidate in enumerate(candidates)
                if source_counts[str(candidate.get("source", ""))] < 1
            ),
            None,
        )

        if chosen_index is None:
            categories.remove(category)
            if not categories:
                break
            cursor %= len(categories)
            continue

        chosen = candidates.pop(chosen_index)
        source_counts[str(chosen.get("source", ""))] += 1
        featured.append(
            {
                **chosen,
                "key": f"featured:{chosen.get('key', '')}",
                "category": "今日精选",
                "featured_rank": len(featured),
            }
        )

        if not candidates:
            categories.remove(category)
            if not categories:
                break
            cursor %= len(categories)
        else:
            cursor += 1

    return featured + items


def esc(value: Any) -> str:
    return html.escape(str(value), quote=True)


def tidy_html(value: str) -> str:
    """Remove indentation-only trailing spaces from generated artifacts."""
    return "\n".join(line.rstrip() for line in value.splitlines()) + "\n"


def ordered_category_names(
    counts: dict[str, int],
    category_order: list[str],
) -> list[str]:
    ordered = [category for category in category_order if category in counts]
    ordered.extend(
        category
        for category in sorted(counts)
        if category not in ordered
    )
    return ordered


def category_selector(
    items: list[dict[str, Any]],
    category_order: list[str],
) -> tuple[str, str]:
    counts: dict[str, int] = defaultdict(int)

    for item in items:
        counts[item.get("category", "未分类")] += 1

    if not counts:
        return "", "__all__"

    categories = ordered_category_names(counts, category_order)
    default_category = categories[0]
    options = []

    for category in categories:
        selected = " selected" if category == default_category else ""
        color = CATEGORY_COLORS.get(category, "#64748b")
        options.append(
            f'<option value="{esc(category)}" '
            f'data-count="{counts[category]}" '
            f'data-color="{esc(color)}"{selected}>'
            f"{esc(category)} · {counts[category]} 条</option>"
        )

    options.append(
        f'<option value="__all__" data-count="{len(items)}" '
        f'data-color="#6366f1">全部内容 · {len(items)} 条</option>'
    )

    selector = f"""
    <label class="select-control" for="category-select">
        <span class="select-label">内容类型</span>
        <span class="select-box">
            <svg aria-hidden="true" viewBox="0 0 24 24">
                <path d="M4 6.5h16M7 12h10M10 17.5h4"/>
            </svg>
            <select
                id="category-select"
                data-default-category="{esc(default_category)}"
                aria-label="选择要查看的内容类型"
            >
                {''.join(options)}
            </select>
            <svg class="select-chevron" aria-hidden="true" viewBox="0 0 24 24">
                <path d="m7 9.5 5 5 5-5"/>
            </svg>
        </span>
    </label>
    """
    return selector, default_category


def edition_switch_script(switch_target: str) -> str:
    target_json = json.dumps(switch_target, ensure_ascii=False)
    return f"""
    <script>
        (() => {{
            const switchTarget = {target_json};
            const switchMark = document.querySelector(".brand-mark");
            let taps = [];

            function switchEdition() {{
                window.location.assign(switchTarget);
            }}

            if (switchMark) {{
                switchMark.addEventListener("click", event => {{
                    event.preventDefault();
                    event.stopPropagation();

                    const now = Date.now();
                    taps = taps.filter(value => now - value <= 4000);
                    taps.push(now);

                    if (taps.length >= 7) {{
                        taps = [];
                        switchEdition();
                    }}
                }});
            }}

            document.addEventListener("keydown", event => {{
                if (
                    event.altKey
                    && event.shiftKey
                    && event.key.toLowerCase() === "g"
                ) {{
                    event.preventDefault();
                    switchEdition();
                }}
            }});
        }})();
    </script>
    """


def render_daily_page(
    *,
    title: str,
    subtitle: str,
    local_date: str,
    items: list[dict[str, Any]],
    errors: list[str],
    local_zone: ZoneInfo,
    category_order: list[str],
    feed_count: int,
    archive_link: str,
    home_link: str,
    generated_at: str,
    edition_id: str,
    switch_target: str,
    noindex: bool,
) -> str:
    grouped: dict[str, list[dict[str, Any]]] = defaultdict(list)

    for item in items:
        grouped[item.get("category", "未分类")].append(item)

    counts = {
        category: len(category_items)
        for category, category_items in grouped.items()
    }
    categories = ordered_category_names(counts, category_order)
    selector, default_category = category_selector(items, category_order)
    sections = []

    for category in categories:
        anchor = hashlib.md5(category.encode("utf-8")).hexdigest()[:10]
        color = CATEGORY_COLORS.get(category, "#475569")
        hidden_attribute = "" if category == default_category else " hidden"
        cards = []

        category_items = (
            sorted(
                grouped[category],
                key=lambda value: int(value.get("featured_rank", 9999)),
            )
            if category == "今日精选"
            else sorted(
                grouped[category],
                key=lambda value: value.get("published", ""),
                reverse=True,
            )
        )

        for item in category_items:
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
            read_more = (
                f'<a class="read-more" href="{link}" target="_blank" '
                f'rel="noopener noreferrer">阅读原文'
                '<svg aria-hidden="true" viewBox="0 0 24 24">'
                '<path d="M5 12h14m-5-5 5 5-5 5"/></svg></a>'
                if link
                else ""
            )

            cards.append(
                f"""
                <article class="card">
                    <div class="card-accent" aria-hidden="true"></div>
                    <div class="card-meta-top">
                        <span class="source-badge">{source}</span>
                        <time datetime="{esc(item.get('published', ''))}">
                            <svg aria-hidden="true" viewBox="0 0 24 24">
                                <circle cx="12" cy="12" r="8"/>
                                <path d="M12 8v4.5l3 1.5"/>
                            </svg>
                            {esc(local_time)}
                        </time>
                    </div>
                    {heading}
                    {summary_html}
                    <div class="card-footer">{read_more}</div>
                </article>
                """
            )

        sections.append(
            f"""
            <section
                class="content-section"
                id="{anchor}"
                data-category="{esc(category)}"
                style="--category-color: {color}"
                {hidden_attribute}
            >
                <div class="section-heading">
                    <div>
                        <span class="section-kicker">CURRENT CHANNEL</span>
                        <h2>{esc(category)}</h2>
                    </div>
                    <span class="section-count">{len(cards)} 条内容</span>
                </div>
                <div class="cards-grid">{''.join(cards)}</div>
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

    default_count = counts.get(default_category, len(items))

    robots_meta = (
        '<meta name="robots" content="noindex,nofollow">'
        if noindex
        else ""
    )
    switch_script = edition_switch_script(switch_target)
    category_storage_key = f"personalDailyCategory:{edition_id}"

    return f"""<!doctype html>
<html lang="zh-CN">
<head>
    <meta charset="utf-8">
    <meta name="viewport" content="width=device-width, initial-scale=1">
    <meta name="color-scheme" content="light dark">
    <meta http-equiv="Cache-Control" content="no-cache, no-store, must-revalidate">
    <meta http-equiv="Pragma" content="no-cache">
    <meta http-equiv="Expires" content="0">
    <meta name="theme-color" content="#090d18">
    {robots_meta}
    <title>{esc(title)} · {esc(local_date)}</title>
    <script>
        try {{
            const savedTheme = localStorage.getItem("personalDailyTheme");
            if (savedTheme === "light" || savedTheme === "dark") {{
                document.documentElement.dataset.theme = savedTheme;
            }}
        }} catch (error) {{}}
    </script>
    <style>
        :root {{
            color-scheme: light;
            --page: #f3f6fc;
            --page-deep: #e9eef8;
            --surface: rgba(255, 255, 255, 0.84);
            --surface-solid: #ffffff;
            --surface-soft: rgba(248, 250, 255, 0.82);
            --text: #162033;
            --text-soft: #344054;
            --muted: #6b7587;
            --border: rgba(37, 56, 88, 0.12);
            --border-strong: rgba(37, 56, 88, 0.20);
            --accent: #4f46e5;
            --accent-two: #0ea5e9;
            --glow: rgba(79, 70, 229, 0.20);
            --shadow: 0 20px 60px rgba(37, 55, 88, 0.10);
            --shadow-card: 0 12px 34px rgba(37, 55, 88, 0.08);
        }}

        :root[data-theme="dark"] {{
            color-scheme: dark;
            --page: #070b13;
            --page-deep: #0b1020;
            --surface: rgba(17, 24, 39, 0.78);
            --surface-solid: #111827;
            --surface-soft: rgba(15, 23, 42, 0.76);
            --text: #f2f6ff;
            --text-soft: #d2d9e6;
            --muted: #929eb2;
            --border: rgba(148, 163, 184, 0.16);
            --border-strong: rgba(148, 163, 184, 0.26);
            --accent: #818cf8;
            --accent-two: #38bdf8;
            --glow: rgba(99, 102, 241, 0.24);
            --shadow: 0 24px 70px rgba(0, 0, 0, 0.30);
            --shadow-card: 0 16px 42px rgba(0, 0, 0, 0.22);
        }}

        @media (prefers-color-scheme: dark) {{
            :root:not([data-theme="light"]) {{
                color-scheme: dark;
                --page: #070b13;
                --page-deep: #0b1020;
                --surface: rgba(17, 24, 39, 0.78);
                --surface-solid: #111827;
                --surface-soft: rgba(15, 23, 42, 0.76);
                --text: #f2f6ff;
                --text-soft: #d2d9e6;
                --muted: #929eb2;
                --border: rgba(148, 163, 184, 0.16);
                --border-strong: rgba(148, 163, 184, 0.26);
                --accent: #818cf8;
                --accent-two: #38bdf8;
                --glow: rgba(99, 102, 241, 0.24);
                --shadow: 0 24px 70px rgba(0, 0, 0, 0.30);
                --shadow-card: 0 16px 42px rgba(0, 0, 0, 0.22);
            }}
        }}

        * {{ box-sizing: border-box; }}
        html {{ scroll-behavior: smooth; }}

        body {{
            min-height: 100vh;
            margin: 0;
            overflow-x: hidden;
            background:
                radial-gradient(circle at 8% 0%, rgba(56, 189, 248, 0.12), transparent 29rem),
                radial-gradient(circle at 92% 10%, var(--glow), transparent 34rem),
                linear-gradient(155deg, var(--page), var(--page-deep));
            color: var(--text);
            font-family: Inter, ui-sans-serif, system-ui, -apple-system,
                BlinkMacSystemFont, "Segoe UI", "Microsoft YaHei", sans-serif;
            line-height: 1.66;
        }}

        body::before {{
            position: fixed;
            inset: 0;
            z-index: -2;
            background-image:
                linear-gradient(rgba(125, 141, 168, 0.055) 1px, transparent 1px),
                linear-gradient(90deg, rgba(125, 141, 168, 0.055) 1px, transparent 1px);
            background-size: 42px 42px;
            mask-image: linear-gradient(to bottom, black, transparent 82%);
            content: "";
        }}

        a {{ color: inherit; text-decoration: none; }}
        button, select {{ font: inherit; }}
        svg {{
            width: 1.1em;
            height: 1.1em;
            fill: none;
            stroke: currentColor;
            stroke-linecap: round;
            stroke-linejoin: round;
            stroke-width: 1.8;
        }}

        .skip-link {{
            position: fixed;
            top: 8px;
            left: 8px;
            z-index: 100;
            padding: 9px 14px;
            border-radius: 10px;
            background: var(--surface-solid);
            color: var(--text);
            transform: translateY(-150%);
        }}

        .skip-link:focus {{ transform: translateY(0); }}

        main {{
            width: min(1160px, calc(100% - 32px));
            margin: 0 auto;
            padding: 28px 0 80px;
        }}

        .hero {{
            position: relative;
            overflow: hidden;
            padding: 26px 30px 32px;
            border: 1px solid var(--border);
            border-radius: 30px;
            background:
                linear-gradient(135deg, rgba(99, 102, 241, 0.11), transparent 48%),
                var(--surface);
            box-shadow: var(--shadow);
            backdrop-filter: blur(22px);
        }}

        .hero::after {{
            position: absolute;
            top: -90px;
            right: -70px;
            width: 320px;
            height: 320px;
            border-radius: 50%;
            background: radial-gradient(circle, rgba(56, 189, 248, 0.20), transparent 68%);
            content: "";
            pointer-events: none;
        }}

        .site-nav,
        .hero-content {{ position: relative; z-index: 1; }}

        .site-nav {{
            display: flex;
            align-items: center;
            justify-content: space-between;
            gap: 18px;
            padding-bottom: 27px;
            border-bottom: 1px solid var(--border);
        }}

        .brand {{
            display: inline-flex;
            align-items: center;
            gap: 11px;
            font-weight: 760;
            letter-spacing: -0.02em;
        }}

        .brand-mark {{
            display: grid;
            width: 38px;
            height: 38px;
            place-items: center;
            border-radius: 12px;
            background: linear-gradient(135deg, #6366f1, #0ea5e9);
            color: #fff;
            box-shadow: 0 10px 24px rgba(79, 70, 229, 0.28);
        }}

        .brand-mark svg {{ width: 21px; height: 21px; }}

        .nav-actions {{ display: flex; align-items: center; gap: 8px; }}

        .nav-link,
        .theme-toggle {{
            display: inline-flex;
            min-height: 38px;
            align-items: center;
            justify-content: center;
            gap: 7px;
            padding: 7px 11px;
            border: 1px solid transparent;
            border-radius: 11px;
            color: var(--muted);
            background: transparent;
            cursor: pointer;
            transition: 160ms ease;
        }}

        .nav-link:hover,
        .theme-toggle:hover {{
            border-color: var(--border);
            background: var(--surface-soft);
            color: var(--text);
            text-decoration: none;
        }}

        .theme-toggle {{ width: 40px; padding: 0; }}
        .theme-toggle span {{ font-size: 18px; line-height: 1; }}

        .hero-content {{
            display: grid;
            grid-template-columns: minmax(0, 1.45fr) minmax(300px, 0.65fr);
            align-items: end;
            gap: 42px;
            padding-top: 38px;
        }}

        .eyebrow {{
            display: inline-flex;
            align-items: center;
            gap: 8px;
            margin-bottom: 14px;
            color: var(--accent-two);
            font-size: 12px;
            font-weight: 800;
            letter-spacing: 0.14em;
            text-transform: uppercase;
        }}

        .live-dot {{
            width: 8px;
            height: 8px;
            border-radius: 999px;
            background: #22c55e;
            box-shadow: 0 0 0 5px rgba(34, 197, 94, 0.13);
        }}

        h1 {{
            max-width: 760px;
            margin: 0;
            font-size: clamp(38px, 6vw, 70px);
            font-weight: 850;
            letter-spacing: -0.065em;
            line-height: 1.02;
        }}

        h1 span {{
            background: linear-gradient(110deg, var(--text) 8%, var(--accent) 58%, var(--accent-two));
            background-clip: text;
            color: transparent;
        }}

        .subtitle {{
            max-width: 670px;
            margin: 18px 0 0;
            color: var(--text-soft);
            font-size: clamp(16px, 2vw, 19px);
        }}

        .hero-meta {{
            display: flex;
            flex-wrap: wrap;
            gap: 10px 20px;
            margin-top: 25px;
            color: var(--muted);
            font-size: 13px;
        }}

        .hero-meta span {{ display: inline-flex; align-items: center; gap: 7px; }}

        .stats {{
            display: grid;
            grid-template-columns: repeat(3, minmax(0, 1fr));
            gap: 10px;
        }}

        .stat {{
            min-width: 0;
            padding: 18px 14px;
            border: 1px solid var(--border);
            border-radius: 17px;
            background: var(--surface-soft);
            text-align: center;
        }}

        .stat strong {{
            display: block;
            color: var(--text);
            font-size: clamp(22px, 3vw, 31px);
            line-height: 1;
        }}

        .stat span {{
            display: block;
            margin-top: 8px;
            color: var(--muted);
            font-size: 11px;
            white-space: nowrap;
        }}

        .filter-panel {{
            --active-color: {esc(CATEGORY_COLORS.get(default_category, '#6366f1'))};
            position: sticky;
            top: 12px;
            z-index: 20;
            display: grid;
            grid-template-columns: minmax(190px, 0.9fr) minmax(280px, 1.15fr) auto;
            align-items: center;
            gap: 20px;
            margin-top: 20px;
            padding: 17px 20px;
            border: 1px solid var(--border-strong);
            border-radius: 20px;
            background: color-mix(in srgb, var(--surface-solid) 82%, transparent);
            box-shadow: var(--shadow-card);
            backdrop-filter: blur(22px);
        }}

        .filter-title {{
            display: block;
            font-size: 16px;
            font-weight: 780;
            letter-spacing: -0.02em;
        }}

        .filter-hint {{ display: block; color: var(--muted); font-size: 12px; }}

        .select-control {{ display: block; min-width: 0; }}
        .select-label {{ position: absolute; width: 1px; height: 1px; overflow: hidden; clip: rect(0, 0, 0, 0); }}

        .select-box {{
            position: relative;
            display: flex;
            align-items: center;
        }}

        .select-box > svg:first-child {{
            position: absolute;
            left: 15px;
            z-index: 1;
            color: var(--active-color);
            pointer-events: none;
        }}

        .select-box select {{
            width: 100%;
            min-height: 48px;
            appearance: none;
            padding: 10px 46px 10px 45px;
            border: 1px solid color-mix(in srgb, var(--active-color) 42%, var(--border));
            border-radius: 14px;
            outline: none;
            background: color-mix(in srgb, var(--active-color) 7%, var(--surface-solid));
            color: var(--text);
            font-weight: 720;
            cursor: pointer;
            transition: 160ms ease;
        }}

        .select-box select:focus {{
            border-color: var(--active-color);
            box-shadow: 0 0 0 4px color-mix(in srgb, var(--active-color) 15%, transparent);
        }}

        .select-chevron {{
            position: absolute;
            right: 14px;
            color: var(--muted);
            pointer-events: none;
        }}

        .current-view {{
            display: flex;
            align-items: center;
            gap: 9px;
            color: var(--muted);
            font-size: 12px;
            white-space: nowrap;
        }}

        .current-view-dot {{
            width: 9px;
            height: 9px;
            border-radius: 50%;
            background: var(--active-color);
            box-shadow: 0 0 0 5px color-mix(in srgb, var(--active-color) 13%, transparent);
        }}

        .current-view strong {{ color: var(--text); font-size: 14px; }}
        .errors {{ margin-top: 20px; }}

        .errors,
        .empty,
        .noscript-message {{
            padding: 18px 20px;
            border: 1px solid var(--border);
            border-radius: 16px;
            background: var(--surface);
        }}

        .errors summary {{ cursor: pointer; font-weight: 720; }}
        .errors li {{ margin: 6px 0; overflow-wrap: anywhere; }}

        .content-section {{
            margin-top: 34px;
            scroll-margin-top: 104px;
        }}

        .content-section[hidden] {{ display: none !important; }}

        .section-heading {{
            display: flex;
            align-items: end;
            justify-content: space-between;
            gap: 20px;
            margin-bottom: 17px;
        }}

        .section-kicker {{
            color: var(--category-color);
            font-size: 10px;
            font-weight: 800;
            letter-spacing: 0.16em;
        }}

        .section-heading h2 {{
            margin: 3px 0 0;
            font-size: clamp(25px, 3vw, 34px);
            letter-spacing: -0.04em;
        }}

        .section-count {{ color: var(--muted); font-size: 13px; }}

        .cards-grid {{
            display: grid;
            grid-template-columns: repeat(2, minmax(0, 1fr));
            gap: 15px;
        }}

        .card {{
            position: relative;
            display: flex;
            min-width: 0;
            min-height: 252px;
            flex-direction: column;
            overflow: hidden;
            padding: 21px 22px 18px;
            border: 1px solid var(--border);
            border-radius: 20px;
            background:
                linear-gradient(145deg, color-mix(in srgb, var(--category-color) 5%, transparent), transparent 42%),
                var(--surface);
            box-shadow: var(--shadow-card);
            transition: transform 180ms ease, border-color 180ms ease, box-shadow 180ms ease;
        }}

        .card:hover {{
            border-color: color-mix(in srgb, var(--category-color) 38%, var(--border));
            box-shadow: 0 20px 46px color-mix(in srgb, var(--category-color) 11%, transparent);
            transform: translateY(-3px);
        }}

        .card-accent {{
            position: absolute;
            top: 0;
            right: 22px;
            left: 22px;
            height: 2px;
            border-radius: 0 0 99px 99px;
            background: linear-gradient(90deg, transparent, var(--category-color), transparent);
            opacity: 0.75;
        }}

        .card-meta-top {{
            display: flex;
            align-items: center;
            justify-content: space-between;
            gap: 12px;
            margin-bottom: 14px;
        }}

        .source-badge {{
            max-width: 68%;
            overflow: hidden;
            padding: 5px 9px;
            border: 1px solid color-mix(in srgb, var(--category-color) 24%, var(--border));
            border-radius: 999px;
            background: color-mix(in srgb, var(--category-color) 8%, var(--surface-solid));
            color: var(--category-color);
            font-size: 11px;
            font-weight: 780;
            text-overflow: ellipsis;
            white-space: nowrap;
        }}

        .card time {{
            display: inline-flex;
            align-items: center;
            gap: 5px;
            color: var(--muted);
            font-size: 11px;
            white-space: nowrap;
        }}

        .card h3 {{
            margin: 0;
            font-size: clamp(17px, 2vw, 20px);
            letter-spacing: -0.02em;
            line-height: 1.42;
        }}

        .card h3 a {{
            color: var(--text);
            transition: color 150ms ease;
        }}

        .card h3 a:hover {{ color: var(--category-color); text-decoration: none; }}

        .summary {{
            display: -webkit-box;
            margin: 11px 0 0;
            overflow: hidden;
            color: var(--text-soft);
            font-size: 14px;
            -webkit-box-orient: vertical;
            -webkit-line-clamp: 4;
        }}

        .card-footer {{
            display: flex;
            align-items: end;
            justify-content: flex-end;
            min-height: 28px;
            margin-top: auto;
            padding-top: 15px;
        }}

        .read-more {{
            display: inline-flex;
            align-items: center;
            gap: 7px;
            color: var(--category-color);
            font-size: 12px;
            font-weight: 760;
        }}

        .read-more svg {{ transition: transform 150ms ease; }}
        .read-more:hover {{ text-decoration: none; }}
        .read-more:hover svg {{ transform: translateX(3px); }}

        .empty {{ margin-top: 24px; }}
        .empty p {{ margin-bottom: 0; color: var(--muted); }}

        footer {{
            display: flex;
            align-items: center;
            justify-content: space-between;
            gap: 20px;
            margin-top: 50px;
            padding: 24px 4px 0;
            border-top: 1px solid var(--border);
            color: var(--muted);
            font-size: 12px;
        }}

        .footer-badge {{
            display: inline-flex;
            align-items: center;
            gap: 7px;
        }}

        @media (max-width: 860px) {{
            .hero-content {{ grid-template-columns: 1fr; gap: 28px; }}
            .stats {{ max-width: 480px; }}
            .filter-panel {{ grid-template-columns: 1fr minmax(260px, 1.3fr); }}
            .current-view {{ display: none; }}
        }}

        @media (max-width: 720px) {{
            main {{ width: min(100% - 20px, 1160px); padding-top: 10px; }}
            .hero {{ padding: 18px 18px 24px; border-radius: 22px; }}
            .site-nav {{ padding-bottom: 18px; }}
            .brand > span:last-child {{ display: none; }}
            .nav-link span {{ display: none; }}
            .nav-link {{ width: 40px; padding: 0; }}
            .hero-content {{ padding-top: 26px; }}
            h1 {{ font-size: clamp(39px, 13vw, 58px); }}
            .filter-panel {{
                top: 8px;
                grid-template-columns: 1fr;
                gap: 11px;
                padding: 13px;
                border-radius: 17px;
            }}
            .filter-copy {{ display: none; }}
            .cards-grid {{ grid-template-columns: 1fr; }}
            .card {{ min-height: 0; }}
            .section-heading {{ align-items: center; }}
            footer {{ align-items: flex-start; flex-direction: column; gap: 7px; }}
        }}

        @media (max-width: 430px) {{
            .hero-meta {{ display: grid; gap: 7px; }}
            .stats {{ gap: 7px; }}
            .stat {{ padding: 14px 8px; }}
            .stat span {{ font-size: 10px; }}
            .card {{ padding: 19px 17px 16px; border-radius: 17px; }}
            .card-meta-top {{ align-items: flex-start; flex-direction: column; gap: 8px; }}
            .source-badge {{ max-width: 100%; }}
        }}

        @media (prefers-reduced-motion: reduce) {{
            *, *::before, *::after {{ scroll-behavior: auto !important; transition: none !important; }}
        }}
    </style>
</head>
<body>
    <a class="skip-link" href="#main-content">跳到主要内容</a>
    <main id="main-content">
        <header class="hero">
            <nav class="site-nav" aria-label="主要导航">
                <a class="brand" href="{esc(home_link)}">
                    <span class="brand-mark" aria-hidden="true">
                        <svg viewBox="0 0 24 24">
                            <path d="M5 5.5h10.5A2.5 2.5 0 0 1 18 8v10.5H7.5A2.5 2.5 0 0 1 5 16V5.5Z"/>
                            <path d="M8.5 9h6M8.5 12h6M8.5 15h3.5M18 9h1a1 1 0 0 1 1 1v6.5a2 2 0 0 1-2 2"/>
                        </svg>
                    </span>
                    <span>今日知览</span>
                </a>
                <div class="nav-actions">
                    <a class="nav-link" href="{esc(home_link)}" title="今天">
                        <svg aria-hidden="true" viewBox="0 0 24 24">
                            <path d="M5 5h14v14H5zM8 3v4M16 3v4M5 9h14"/>
                        </svg>
                        <span>今天</span>
                    </a>
                    <a class="nav-link" href="{esc(archive_link)}" title="历史归档">
                        <svg aria-hidden="true" viewBox="0 0 24 24">
                            <path d="M4 7h16M6 4h12l1 3v13H5V7l1-3ZM9 11h6"/>
                        </svg>
                        <span>归档</span>
                    </a>
                    <button class="theme-toggle" id="theme-toggle" type="button" title="切换明暗主题" aria-label="切换明暗主题">
                        <span id="theme-icon" aria-hidden="true">☾</span>
                    </button>
                </div>
            </nav>

            <div class="hero-content">
                <div>
                    <span class="eyebrow"><span class="live-dot"></span> DAILY BRIEFING</span>
                    <h1><span>{esc(title)}</span></h1>
                    <p class="subtitle">{esc(subtitle)}</p>
                    <div class="hero-meta">
                        <span>
                            <svg aria-hidden="true" viewBox="0 0 24 24">
                                <path d="M5 5h14v14H5zM8 3v4M16 3v4M5 9h14"/>
                            </svg>
                            {esc(local_date)}
                        </span>
                        <span>
                            <svg aria-hidden="true" viewBox="0 0 24 24">
                                <path d="M12 3v3M12 18v3M4.2 7.5l2.6 1.5M17.2 15l2.6 1.5M4.2 16.5 6.8 15M17.2 9l2.6-1.5"/>
                                <circle cx="12" cy="12" r="4"/>
                            </svg>
                            每日 08:30 自动更新
                        </span>
                    </div>
                </div>
                <div class="stats" aria-label="日报统计">
                    <div class="stat"><strong>{len(items)}</strong><span>今日收录</span></div>
                    <div class="stat"><strong>{feed_count}</strong><span>免费信息源</span></div>
                    <div class="stat"><strong>{len(categories)}</strong><span>内容频道</span></div>
                </div>
            </div>
        </header>

        {error_html}

        <div class="filter-panel" id="filter-panel">
            <div class="filter-copy">
                <span class="filter-title">你想看什么？</span>
                <span class="filter-hint">切换后只显示所选频道</span>
            </div>
            {selector}
            <div class="current-view" aria-live="polite">
                <span class="current-view-dot" aria-hidden="true"></span>
                <span id="active-category-name">{esc(default_category)}</span>
                <strong id="active-category-count">{default_count} 条</strong>
            </div>
        </div>

        <noscript>
            <style>.content-section[hidden] {{ display: block !important; }}</style>
            <p class="noscript-message">启用 JavaScript 后可以使用分类下拉筛选；当前仍会显示全部内容。</p>
        </noscript>

        {''.join(sections)}

        <footer>
            <span>最后生成：{esc(generated_at)}</span>
            <span class="footer-badge"><span class="live-dot"></span> GitHub Actions 自动更新</span>
        </footer>
    </main>

    <script>
        (() => {{
            const root = document.documentElement;
            const selector = document.getElementById("category-select");
            const sections = Array.from(document.querySelectorAll(".content-section"));
            const filterPanel = document.getElementById("filter-panel");
            const activeName = document.getElementById("active-category-name");
            const activeCount = document.getElementById("active-category-count");
            const themeToggle = document.getElementById("theme-toggle");
            const themeIcon = document.getElementById("theme-icon");

            const values = new Set(Array.from(selector.options, option => option.value));
            const url = new URL(window.location.href);
            const requested = url.searchParams.get("category");
            let savedCategory = null;

            try {{
                savedCategory = localStorage.getItem("{esc(category_storage_key)}");
            }} catch (error) {{}}

            const initialCategory = values.has(requested)
                ? requested
                : values.has(savedCategory)
                    ? savedCategory
                    : selector.dataset.defaultCategory;

            function applyCategory(value, updateHistory = false, shouldScroll = false) {{
                const selectedOption = Array.from(selector.options).find(
                    option => option.value === value
                );
                const showAll = value === "__all__";

                sections.forEach(section => {{
                    section.hidden = !showAll && section.dataset.category !== value;
                }});

                selector.value = value;
                activeName.textContent = showAll ? "全部内容" : value;
                activeCount.textContent = (selectedOption?.dataset.count || "0") + " 条";
                filterPanel.style.setProperty(
                    "--active-color",
                    selectedOption?.dataset.color || "#6366f1"
                );

                try {{
                    localStorage.setItem("{esc(category_storage_key)}", value);
                }} catch (error) {{}}

                if (updateHistory) {{
                    if (value === selector.dataset.defaultCategory) {{
                        url.searchParams.delete("category");
                    }} else {{
                        url.searchParams.set("category", value);
                    }}
                    history.replaceState(null, "", url);
                }}

                if (shouldScroll) {{
                    const top = filterPanel.getBoundingClientRect().top + window.scrollY - 10;
                    window.scrollTo({{ top, behavior: "smooth" }});
                }}
            }}

            selector.addEventListener("change", () => {{
                applyCategory(selector.value, true, true);
            }});

            function isDarkTheme() {{
                if (root.dataset.theme) return root.dataset.theme === "dark";
                return window.matchMedia("(prefers-color-scheme: dark)").matches;
            }}

            function refreshThemeIcon() {{
                const dark = isDarkTheme();
                themeIcon.textContent = dark ? "☀" : "☾";
                themeToggle.title = dark ? "切换到浅色主题" : "切换到深色主题";
                themeToggle.setAttribute("aria-label", themeToggle.title);
            }}

            themeToggle.addEventListener("click", () => {{
                const nextTheme = isDarkTheme() ? "light" : "dark";
                root.dataset.theme = nextTheme;
                try {{
                    localStorage.setItem("personalDailyTheme", nextTheme);
                }} catch (error) {{}}
                refreshThemeIcon();
            }});

            applyCategory(initialCategory);
            refreshThemeIcon();
        }})();
    </script>
    {switch_script}
</body>
</html>
"""


def render_archive_index(
    *,
    title: str,
    dates: list[str],
    home_link: str,
    switch_target: str,
    noindex: bool,
) -> str:
    cards = "\n".join(
        f"""
        <a class="archive-card" href="{esc(value)}.html">
            <span class="archive-icon" aria-hidden="true">
                <svg viewBox="0 0 24 24">
                    <path d="M5 5h14v14H5zM8 3v4M16 3v4M5 9h14"/>
                </svg>
            </span>
            <span>
                <strong>{esc(value)}</strong>
                <small>查看当日完整信息流</small>
            </span>
            <svg class="arrow" aria-hidden="true" viewBox="0 0 24 24">
                <path d="M5 12h14m-5-5 5 5-5 5"/>
            </svg>
        </a>
        """
        for value in dates
    )
    if not cards:
        cards = """
        <div class="empty">
            <strong>暂时还没有历史归档</strong>
            <p>日报首次运行后，日期会自动出现在这里。</p>
        </div>
        """

    robots_meta = (
        '<meta name="robots" content="noindex,nofollow">'
        if noindex
        else ""
    )
    switch_script = edition_switch_script(switch_target)

    return f"""<!doctype html>
<html lang="zh-CN">
<head>
    <meta charset="utf-8">
    <meta name="viewport" content="width=device-width, initial-scale=1">
    <meta name="color-scheme" content="light dark">
    <meta http-equiv="Cache-Control" content="no-cache, no-store, must-revalidate">
    <meta name="theme-color" content="#090d18">
    {robots_meta}
    <title>历史归档 · {esc(title)}</title>
    <script>
        try {{
            const savedTheme = localStorage.getItem("personalDailyTheme");
            if (savedTheme === "light" || savedTheme === "dark") {{
                document.documentElement.dataset.theme = savedTheme;
            }}
        }} catch (error) {{}}
    </script>
    <style>
        :root {{
            color-scheme: light;
            --page: #f3f6fc;
            --surface: rgba(255, 255, 255, 0.84);
            --surface-solid: #ffffff;
            --text: #162033;
            --muted: #667085;
            --border: rgba(100, 116, 139, 0.2);
            --shadow: 0 24px 80px rgba(39, 51, 89, 0.12);
            --accent: #6366f1;
            --accent-2: #06b6d4;
        }}
        :root[data-theme="dark"] {{
            color-scheme: dark;
            --page: #090d18;
            --surface: rgba(18, 25, 41, 0.84);
            --surface-solid: #121929;
            --text: #f2f5fb;
            --muted: #98a6bd;
            --border: rgba(148, 163, 184, 0.18);
            --shadow: 0 28px 90px rgba(0, 0, 0, 0.36);
        }}
        @media (prefers-color-scheme: dark) {{
            :root:not([data-theme="light"]) {{
                color-scheme: dark;
                --page: #090d18;
                --surface: rgba(18, 25, 41, 0.84);
                --surface-solid: #121929;
                --text: #f2f5fb;
                --muted: #98a6bd;
                --border: rgba(148, 163, 184, 0.18);
                --shadow: 0 28px 90px rgba(0, 0, 0, 0.36);
            }}
        }}
        * {{ box-sizing: border-box; }}
        html {{ min-height: 100%; }}
        body {{
            min-height: 100vh;
            margin: 0;
            font-family: ui-sans-serif, system-ui, -apple-system, "Segoe UI", sans-serif;
            color: var(--text);
            background:
                radial-gradient(circle at 12% 4%, rgba(99, 102, 241, 0.18), transparent 30rem),
                radial-gradient(circle at 88% 18%, rgba(6, 182, 212, 0.13), transparent 26rem),
                var(--page);
            line-height: 1.5;
        }}
        body::before {{
            position: fixed;
            inset: 0;
            z-index: -1;
            content: "";
            opacity: 0.25;
            background-image:
                linear-gradient(var(--border) 1px, transparent 1px),
                linear-gradient(90deg, var(--border) 1px, transparent 1px);
            background-size: 38px 38px;
            mask-image: linear-gradient(to bottom, black, transparent 72%);
        }}
        a {{ color: inherit; text-decoration: none; }}
        svg {{
            width: 20px;
            height: 20px;
            fill: none;
            stroke: currentColor;
            stroke-width: 1.8;
            stroke-linecap: round;
            stroke-linejoin: round;
        }}
        main {{
            width: min(900px, calc(100% - 32px));
            margin: 0 auto;
            padding: 28px 0 64px;
        }}
        .hero {{
            position: relative;
            overflow: hidden;
            padding: 26px clamp(22px, 5vw, 48px) clamp(30px, 6vw, 58px);
            border: 1px solid var(--border);
            border-radius: 30px;
            background: var(--surface);
            box-shadow: var(--shadow);
            backdrop-filter: blur(22px);
        }}
        .hero::after {{
            position: absolute;
            top: -110px;
            right: -90px;
            width: 300px;
            height: 300px;
            border-radius: 50%;
            content: "";
            background: linear-gradient(135deg, rgba(99, 102, 241, 0.32), rgba(6, 182, 212, 0.08));
            filter: blur(5px);
        }}
        nav {{
            position: relative;
            z-index: 1;
            display: flex;
            align-items: center;
            justify-content: space-between;
            gap: 16px;
        }}
        .brand {{ display: flex; align-items: center; gap: 11px; font-weight: 800; }}
        .brand-mark,
        .archive-icon {{
            display: grid;
            flex: 0 0 auto;
            place-items: center;
            width: 42px;
            height: 42px;
            border-radius: 14px;
            color: white;
            background: linear-gradient(135deg, var(--accent), var(--accent-2));
            box-shadow: 0 12px 28px rgba(99, 102, 241, 0.24);
        }}
        .nav-actions {{ display: flex; align-items: center; gap: 8px; }}
        .home-link,
        .theme-toggle {{
            display: inline-flex;
            align-items: center;
            justify-content: center;
            min-height: 42px;
            padding: 0 15px;
            border: 1px solid var(--border);
            border-radius: 13px;
            color: var(--muted);
            background: var(--surface-solid);
            cursor: pointer;
        }}
        .home-link:hover,
        .theme-toggle:hover {{ color: var(--text); border-color: rgba(99, 102, 241, 0.45); }}
        .theme-toggle {{ width: 42px; padding: 0; font-size: 18px; }}
        .hero-copy {{ position: relative; z-index: 1; margin-top: 74px; }}
        .eyebrow {{
            display: inline-flex;
            align-items: center;
            gap: 8px;
            color: var(--accent);
            font-size: 12px;
            font-weight: 800;
            letter-spacing: 0.12em;
            text-transform: uppercase;
        }}
        .eyebrow::before {{
            width: 8px;
            height: 8px;
            border-radius: 50%;
            content: "";
            background: #22c55e;
            box-shadow: 0 0 0 5px rgba(34, 197, 94, 0.12);
        }}
        h1 {{ margin: 14px 0 10px; font-size: clamp(35px, 7vw, 62px); letter-spacing: -0.055em; line-height: 1; }}
        .lead {{ margin: 0; color: var(--muted); font-size: clamp(15px, 2vw, 18px); }}
        .archive-heading {{
            display: flex;
            align-items: end;
            justify-content: space-between;
            gap: 18px;
            margin: 42px 4px 18px;
        }}
        .archive-heading h2 {{ margin: 0; font-size: 22px; letter-spacing: -0.025em; }}
        .archive-heading span {{ color: var(--muted); font-size: 13px; }}
        .archive-grid {{ display: grid; grid-template-columns: repeat(2, minmax(0, 1fr)); gap: 14px; }}
        .archive-card {{
            display: grid;
            grid-template-columns: auto minmax(0, 1fr) auto;
            align-items: center;
            gap: 15px;
            min-width: 0;
            padding: 18px;
            border: 1px solid var(--border);
            border-radius: 20px;
            background: var(--surface);
            box-shadow: 0 12px 38px rgba(39, 51, 89, 0.07);
            backdrop-filter: blur(16px);
            transition: transform 160ms ease, border-color 160ms ease, box-shadow 160ms ease;
        }}
        .archive-card:hover {{
            transform: translateY(-3px);
            border-color: rgba(99, 102, 241, 0.42);
            box-shadow: 0 18px 44px rgba(39, 51, 89, 0.13);
        }}
        .archive-icon {{ width: 44px; height: 44px; border-radius: 14px; }}
        .archive-icon svg {{ width: 19px; height: 19px; }}
        .archive-card strong {{ display: block; font-size: 16px; letter-spacing: -0.01em; }}
        .archive-card small {{ display: block; margin-top: 3px; color: var(--muted); font-size: 12px; }}
        .archive-card .arrow {{ color: var(--muted); transition: transform 160ms ease; }}
        .archive-card:hover .arrow {{ color: var(--accent); transform: translateX(3px); }}
        .empty {{
            grid-column: 1 / -1;
            padding: 28px;
            border: 1px solid var(--border);
            border-radius: 20px;
            background: var(--surface);
        }}
        .empty p {{ margin: 6px 0 0; color: var(--muted); }}
        footer {{ margin-top: 32px; color: var(--muted); text-align: center; font-size: 12px; }}
        @media (max-width: 650px) {{
            main {{ width: min(100% - 20px, 900px); padding-top: 10px; }}
            .hero {{ padding: 18px 18px 34px; border-radius: 23px; }}
            .brand span:last-child {{ display: none; }}
            .home-link {{ padding: 0 12px; }}
            .hero-copy {{ margin-top: 52px; }}
            .archive-grid {{ grid-template-columns: 1fr; }}
            .archive-heading {{ margin-top: 32px; }}
        }}
        @media (prefers-reduced-motion: reduce) {{
            *, *::before, *::after {{ scroll-behavior: auto !important; transition: none !important; }}
        }}
    </style>
</head>
<body>
    <main>
        <header class="hero">
            <nav>
                <a class="brand" href="{esc(home_link)}">
                    <span class="brand-mark" aria-hidden="true">
                        <svg viewBox="0 0 24 24"><path d="M4 17V7l8-4 8 4v10l-8 4-8-4Z"/><path d="m8 9 4 2 4-2M12 11v6"/></svg>
                    </span>
                    <span>今日知览</span>
                </a>
                <div class="nav-actions">
                    <a class="home-link" href="{esc(home_link)}">返回今天</a>
                    <button class="theme-toggle" id="theme-toggle" type="button" aria-label="切换主题"><span id="theme-icon">☾</span></button>
                </div>
            </nav>
            <div class="hero-copy">
                <span class="eyebrow">Timeline library</span>
                <h1>历史归档</h1>
                <p class="lead">回到某一天，继续浏览当时收录的重要信息。</p>
            </div>
        </header>
        <div class="archive-heading">
            <h2>所有日报</h2>
            <span>{len(dates)} 个归档日期</span>
        </div>
        <div class="archive-grid">{cards}</div>
        <footer>{esc(title)} · 由 GitHub Actions 自动维护</footer>
    </main>
    <script>
        (() => {{
            const root = document.documentElement;
            const toggle = document.getElementById("theme-toggle");
            const icon = document.getElementById("theme-icon");
            const isDark = () => root.dataset.theme
                ? root.dataset.theme === "dark"
                : window.matchMedia("(prefers-color-scheme: dark)").matches;
            const refresh = () => {{
                icon.textContent = isDark() ? "☀" : "☾";
                toggle.title = isDark() ? "切换到浅色主题" : "切换到深色主题";
            }};
            toggle.addEventListener("click", () => {{
                root.dataset.theme = isDark() ? "light" : "dark";
                try {{ localStorage.setItem("personalDailyTheme", root.dataset.theme); }} catch (error) {{}}
                refresh();
            }});
            refresh();
        }})();
    </script>
    {switch_script}
</body>
</html>
"""


def clean_archive_directory(archive_path: Path, dates: list[str]) -> None:
    valid_names = {"index.html"}
    valid_names.update(f"{value}.html" for value in dates)

    for path in archive_path.glob("*.html"):
        if path.name not in valid_names:
            path.unlink()


def generate_edition(
    *,
    name: str,
    edition_id: str,
    config_path: Path,
    data_path: Path,
    site_path: Path,
    now_utc: datetime,
    root_switch_target: str,
    archive_switch_target: str,
    noindex: bool,
    legacy_data_path: Path | None = None,
) -> None:
    config = load_json(config_path, {})
    history_path = data_path
    if not data_path.exists() and legacy_data_path and legacy_data_path.exists():
        history_path = legacy_data_path
    history = load_json(history_path, [])

    title = config.get("title", "今日知览")
    subtitle = config.get("subtitle", "每日自动更新")
    timezone_name = config.get("timezone", "Asia/Shanghai")
    lookback_hours = int(config.get("lookback_hours", 48))
    retention_days = int(config.get("retention_days", 180))
    max_items_per_feed = int(config.get("max_items_per_feed", 5))
    max_daily_items = int(config.get("max_daily_items", 100))
    max_items_per_category = int(config.get("max_items_per_category", 15))
    max_featured_items = int(config.get("max_featured_items", 10))
    feeds = config.get("feeds", [])
    category_order = config.get("category_order") or list(
        dict.fromkeys(
            source.get("category", "未分类")
            for source in feeds
        )
    )

    local_zone = ZoneInfo(timezone_name)
    now_local = now_utc.astimezone(local_zone)
    today = now_local.date().isoformat()
    cutoff = now_utc - timedelta(hours=lookback_hours)

    session = requests.Session()
    session.headers.update(
        {
            "User-Agent": (
                "personal-daily/2.0 "
                "(+https://github.com; personal news reader)"
            ),
            "Accept": "application/rss+xml, application/atom+xml, "
            "application/xml, text/xml, text/html;q=0.9, */*;q=0.5",
            "Accept-Language": "zh-CN,zh;q=0.9,en;q=0.7",
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

    print()
    print(f"=== {name} ===")
    errors: list[str] = []
    fetched_count = 0

    for source in feeds:
        try:
            fetched = fetch_source(
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
        except Exception as error:  # Continue when one external source is down.
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
    today_items = select_daily_items(
        retained=retained,
        today=today,
        category_order=category_order,
        max_daily_items=max_daily_items,
        max_items_per_category=max_items_per_category,
    )
    today_page_items = add_featured_items(
        today_items,
        category_order,
        max_featured_items,
    )
    generated_at = now_local.strftime("%Y-%m-%d %H:%M %Z")
    archive_path = site_path / "archive"
    site_path.mkdir(parents=True, exist_ok=True)
    archive_path.mkdir(parents=True, exist_ok=True)

    (site_path / "index.html").write_text(
        tidy_html(
            render_daily_page(
                title=title,
                subtitle=subtitle,
                local_date=today,
                items=today_page_items,
                errors=errors,
                local_zone=local_zone,
                category_order=category_order,
                feed_count=len(feeds),
                archive_link="archive/index.html",
                home_link="index.html",
                generated_at=generated_at,
                edition_id=edition_id,
                switch_target=root_switch_target,
                noindex=noindex,
            )
        ),
        encoding="utf-8",
    )

    archive_dates = sorted(
        {
            str(item.get("first_seen"))
            for item in retained
            if item.get("first_seen")
        },
        reverse=True,
    )

    for archive_date in archive_dates:
        archive_items = select_daily_items(
            retained=retained,
            today=archive_date,
            category_order=category_order,
            max_daily_items=max_daily_items,
            max_items_per_category=max_items_per_category,
        )
        archive_page_items = add_featured_items(
            archive_items,
            category_order,
            max_featured_items,
        )
        (archive_path / f"{archive_date}.html").write_text(
            tidy_html(
                render_daily_page(
                    title=title,
                    subtitle=subtitle,
                    local_date=archive_date,
                    items=archive_page_items,
                    errors=errors if archive_date == today else [],
                    local_zone=local_zone,
                    category_order=category_order,
                    feed_count=len(feeds),
                    archive_link="index.html",
                    home_link="../index.html",
                    generated_at=generated_at,
                    edition_id=edition_id,
                    switch_target=archive_switch_target,
                    noindex=noindex,
                )
            ),
            encoding="utf-8",
        )

    clean_archive_directory(archive_path, archive_dates)
    (archive_path / "index.html").write_text(
        tidy_html(
            render_archive_index(
                title=title,
                dates=archive_dates,
                home_link="../index.html",
                switch_target=archive_switch_target,
                noindex=noindex,
            )
        ),
        encoding="utf-8",
    )
    save_json(data_path, retained)

    print(f"Date: {today}")
    print(f"Sources configured: {len(feeds)}")
    print(f"Items fetched in window: {fetched_count}")
    print(f"Items shown today: {len(today_items)} + {len(today_page_items) - len(today_items)} featured")
    print(f"Source errors: {len(errors)}")


def main() -> None:
    now_utc = datetime.now(timezone.utc)
    generate_edition(
        name="大陆版",
        edition_id="cn",
        config_path=DOMESTIC_CONFIG_PATH,
        data_path=DOMESTIC_DATA_PATH,
        site_path=SITE_PATH,
        now_utc=now_utc,
        root_switch_target="view/2/index.html",
        archive_switch_target="../view/2/index.html",
        noindex=False,
    )
    generate_edition(
        name="国际版",
        edition_id="global",
        config_path=GLOBAL_CONFIG_PATH,
        data_path=GLOBAL_DATA_PATH,
        legacy_data_path=LEGACY_GLOBAL_DATA_PATH,
        site_path=GLOBAL_SITE_PATH,
        now_utc=now_utc,
        root_switch_target="../../index.html",
        archive_switch_target="../../../index.html",
        noindex=True,
    )


if __name__ == "__main__":
    main()

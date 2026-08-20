#!/usr/bin/env python3
"""
Chaarcha RSS Feed Generator
────────────────────────────
Scrapes category pages and extracts article metadata from the Next.js App
Router RSC flight-data payloads (self.__next_f.push).

Why not the API?
  https://api.chaarcha.com/api/v2/home/ now enters a 30-redirect loop.
  The HTML pages embed the full categoryAllStories JSON in the RSC stream,
  giving us everything we need: slug, title, excerpt, thumbnail, pub-date.

No full-article fetch. No seen.json. Just:
  Outputs: explainer.xml  analysis.xml  thoughts.xml  index.html
"""

import re
import json
import time
import html as html_mod
import requests
from requests.exceptions import TooManyRedirects
from datetime import datetime, timezone
from feedgen.feed import FeedGenerator


# ── config ─────────────────────────────────────────────────────────────────
SITE_BASE    = "https://www.chaarcha.com"
MAX_ARTICLES = 20

CATEGORIES = [
    ("explainer", "এক্সপ্লেইনার"),
    ("analysis",  "বিশ্লেষণ"),
    ("thoughts",  "ভাবনা"),
]

HEADERS = {
    "User-Agent": (
        "Mozilla/5.0 (Windows NT 10.0; Win64; x64) "
        "AppleWebKit/537.36 (KHTML, like Gecko) "
        "Chrome/124.0.0.0 Safari/537.36"
    ),
    "Accept":          "text/html,application/xhtml+xml,application/xml;q=0.9,*/*;q=0.8",
    "Accept-Language": "bn-BD,bn;q=0.9,en-US;q=0.8",
    "Referer":         "https://www.chaarcha.com/",
}


# ── HTTP ───────────────────────────────────────────────────────────────────
def http_get(url: str, retries: int = 3) -> str | None:
    """
    Fresh session per call — avoids cookie-induced redirect loops.
    Bails immediately on redirect loops instead of hitting the 30-redirect wall.
    """
    for attempt in range(retries):
        try:
            sess = requests.Session()
            sess.max_redirects = 5
            r = sess.get(url, headers=HEADERS, timeout=30)
            r.raise_for_status()
            return r.text
        except TooManyRedirects:
            print(f"  [warn] redirect loop — {url}")
            return None          # no point retrying
        except Exception as exc:
            print(f"  [warn] attempt {attempt + 1} — {exc}")
            time.sleep(3 * (attempt + 1))
    print(f"  [error] gave up on {url}")
    return None


# ── RSC parser ─────────────────────────────────────────────────────────────
def _decode_rsc_chunks(html: str) -> str:
    """
    Collect all self.__next_f.push([1, "..."]) payloads and decode the
    JSON-string escaping (\\n, \\", etc.) into plain text we can search.
    """
    raw_chunks = re.findall(
        r'self\.__next_f\.push\(\[1,"(.*?)"\]\)',
        html,
        re.DOTALL,
    )
    parts: list[str] = []
    for raw in raw_chunks:
        try:
            parts.append(json.loads(f'"{raw}"'))
        except Exception:
            parts.append(raw)   # fall back to raw if decode fails
    return "\n".join(parts)


def _extract_json_array(text: str, key: str) -> list:
    """
    Locate `key` in `text` and extract the JSON array that follows it
    using bracket-depth matching (safe against nested arrays).
    """
    idx = text.find(f'"{key}"')
    if idx == -1:
        return []
    arr_start = text.find('[', idx)
    if arr_start == -1:
        return []

    depth = 0
    for i in range(arr_start, len(text)):
        c = text[i]
        if c == '[':
            depth += 1
        elif c == ']':
            depth -= 1
            if depth == 0:
                try:
                    return json.loads(text[arr_start : i + 1])
                except Exception as exc:
                    print(f"  [error] JSON array parse: {exc}")
                    return []
    return []


def fetch_stories(category_slug: str) -> list[dict]:
    """Fetch category page and extract the categoryAllStories list from RSC data."""
    html = http_get(f"{SITE_BASE}/{category_slug}")
    if not html:
        return []

    rsc_text = _decode_rsc_chunks(html)
    stories  = _extract_json_array(rsc_text, "categoryAllStories")

    if not stories:
        print(f"  [warn] categoryAllStories not found in RSC data for /{category_slug}")
    else:
        print(f"  RSC: {len(stories)} articles found")

    return stories


# ── RSS builder ────────────────────────────────────────────────────────────
def _pub_dt(story: dict) -> datetime:
    raw = story.get("first_published_at") or story.get("last_published_at", "")
    if raw:
        try:
            return datetime.fromisoformat(raw.replace("Z", "+00:00"))
        except Exception:
            pass
    return datetime.now(timezone.utc)


def _description(story: dict, url: str, title: str) -> str:
    img  = story.get("blog_image") or {}
    # API uses 'url'; older seen.json used 'download_url'
    thumb   = img.get("url") or img.get("download_url", "")
    caption = img.get("caption", "")
    excerpt = story.get("excerpt", "")
    author  = (story.get("author") or {}).get("name", "")

    parts: list[str] = []

    if thumb:
        alt = html_mod.escape(title)
        parts.append(
            f'<figure>'
            f'<img src="{thumb}" alt="{alt}" style="max-width:100%;border-radius:8px">'
            f'</figure>'
        )
        if caption:
            parts.append(f'<p><small>{html_mod.escape(caption)}</small></p>')

    if excerpt:
        parts.append(f"<p>{html_mod.escape(excerpt)}</p>")

    if author:
        parts.append(f"<p><em>— {html_mod.escape(author)}</em></p>")

    parts.append(f'<p><a href="{url}">চরচায় পড়ুন →</a></p>')
    return "\n".join(parts)


def build_rss(
    stories: list[dict],
    cat: str,
    title_bn: str,
    out_file: str,
) -> None:
    fg = FeedGenerator()
    fg.id(f"{SITE_BASE}/{cat}")
    fg.title(f"{title_bn} | চরচা")
    fg.link(href=f"{SITE_BASE}/{cat}",      rel="alternate")
    fg.link(href=f"{SITE_BASE}/{cat}.xml",  rel="self")
    fg.language("bn")
    fg.description(f"চরচা — {title_bn}")

    added = 0
    for story in stories:
        if added >= MAX_ARTICLES:
            break

        slug = story.get("news_slug") or story.get("slug", "")
        if not slug:
            continue

        title = story.get("title") or "(শিরোনাম নেই)"
        url   = f"{SITE_BASE}/{cat}/{slug}"

        fe = fg.add_entry()
        fe.id(url)
        fe.title(title)
        fe.link(href=url)
        fe.published(_pub_dt(story))
        fe.updated(_pub_dt(story))
        fe.description(_description(story, url, title))
        added += 1

    fg.rss_file(out_file, pretty=True)
    print(f"  → {out_file} ({added} items)")


# ── index.html ─────────────────────────────────────────────────────────────
def write_index(counts: dict[str, int]) -> None:
    now = datetime.now(timezone.utc).strftime("%Y-%m-%d %H:%M UTC")

    cards = ""
    for slug, title_bn in CATEGORIES:
        n = counts.get(slug, 0)
        cards += f"""
    <div class="card">
      <div class="icon">📰</div>
      <h2>{title_bn}</h2>
      <p class="count">{n} articles</p>
      <p class="src">
        <a href="{SITE_BASE}/{slug}" target="_blank">{SITE_BASE}/{slug}</a>
      </p>
      <div class="btns">
        <a class="btn-r" href="{slug}.xml">📡 RSS</a>
        <a class="btn-w" href="{SITE_BASE}/{slug}" target="_blank">🌐 Source</a>
      </div>
    </div>"""

    page = f"""<!DOCTYPE html>
<html lang="bn">
<head>
<meta charset="UTF-8">
<meta name="viewport" content="width=device-width,initial-scale=1">
<title>Chaarcha RSS Feeds</title>
<style>
  :root{{--b:#274e8f;--bg:#f0f4fb}}
  *{{box-sizing:border-box;margin:0;padding:0}}
  body{{font-family:-apple-system,BlinkMacSystemFont,'Segoe UI',sans-serif;
        background:var(--bg);color:#222;padding:2rem}}
  header{{max-width:820px;margin:0 auto 2rem}}
  h1{{color:var(--b);font-size:1.8rem;margin-bottom:.3rem}}
  .meta{{color:#666;font-size:.85rem;margin-top:.25rem}}
  .grid{{display:grid;grid-template-columns:repeat(auto-fit,minmax(230px,1fr));
         gap:1rem;max-width:820px;margin:0 auto}}
  .card{{background:#fff;border-radius:10px;padding:1.4rem;
         box-shadow:0 2px 8px rgba(0,0,0,.07);
         display:flex;flex-direction:column;gap:.5rem}}
  .icon{{font-size:1.8rem}}
  .card h2{{color:var(--b);font-size:1.1rem}}
  .count,.src{{font-size:.75rem;color:#999}}
  .src a{{color:#999;text-decoration:none}}
  .src a:hover{{text-decoration:underline}}
  .btns{{display:flex;gap:.5rem;margin-top:auto;padding-top:.6rem}}
  .btn-r,.btn-w{{padding:.35rem .9rem;border-radius:6px;
                 text-decoration:none;font-size:.85rem;font-weight:600}}
  .btn-r{{background:var(--b);color:#fff}}
  .btn-w{{background:#e8eef7;color:var(--b)}}
  .btn-r:hover{{background:#1a3a6b}}
  .btn-w:hover{{background:#ccd8ee}}
  footer{{text-align:center;margin-top:2.5rem;font-size:.75rem;color:#aaa}}
  footer a{{color:#aaa}}
</style>
</head>
<body>
<header>
  <h1>চরচা RSS</h1>
  <p class="meta">Updated: {now} &nbsp;·&nbsp; refreshes every 4 h</p>
</header>
<div class="grid">{cards}
</div>
<footer>
  <a href="https://www.chaarcha.com" target="_blank">chaarcha.com</a>
</footer>
</body>
</html>"""

    with open("index.html", "w", encoding="utf-8") as f:
        f.write(page)
    print("  → index.html")


# ── main ───────────────────────────────────────────────────────────────────
def main() -> None:
    counts: dict[str, int] = {}

    for cat, title_bn in CATEGORIES:
        print(f"\n[{cat}]")
        stories  = fetch_stories(cat)
        n        = min(len(stories), MAX_ARTICLES)
        counts[cat] = n
        print(f"  using {n} of {len(stories)}")
        build_rss(stories, cat, title_bn, f"{cat}.xml")
        time.sleep(1)

    write_index(counts)
    print("\n✓ done.")


if __name__ == "__main__":
    main()

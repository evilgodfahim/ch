#!/usr/bin/env python3
"""
Chaarcha RSS Feed Generator
Fetches thoughts / analysis / explainer from chaarcha.com via FlareSolverr.
Outputs:  explainer.xml  analysis.xml  thoughts.xml  index.html  seen.json

seen.json stores already-fetched article content so each slug is only
fetched via FlareSolverr once across all runs.
"""
import os, json, time, html as html_mod
import requests
from datetime import datetime, timezone
from bs4 import BeautifulSoup
from feedgen.feed import FeedGenerator

# ── config ────────────────────────────────────────────────────────────────────
FLARESOLVERR  = os.environ.get("FLARESOLVERR_URL", "http://localhost:8191/v1")
API_BASE      = "https://api.chaarcha.com/api/v2/home/"
SITE_BASE     = "https://www.chaarcha.com"
MAX_ARTICLES  = 20   # per feed (most-recent N articles in RSS)
MAX_API_PAGES = 2    # 15 articles/page → up to 30 candidates
SEEN_FILE     = "seen.json"

CATEGORIES = [
    ("explainer", "এক্সপ্লেইনার"),
    ("analysis",  "বিশ্লেষণ"),
    ("thoughts",  "ভাবনা"),
]

# ── seen-cache helpers ────────────────────────────────────────────────────────
def load_seen() -> dict:
    """Return {slug: {"content": str, "fetched_at": str}}"""
    if os.path.exists(SEEN_FILE):
        try:
            with open(SEEN_FILE, encoding="utf-8") as f:
                return json.load(f)
        except Exception as exc:
            print(f"[warn] could not load {SEEN_FILE}: {exc}")
    return {}


def save_seen(seen: dict) -> None:
    with open(SEEN_FILE, "w", encoding="utf-8") as f:
        json.dump(seen, f, ensure_ascii=False, indent=2)


# ── FlareSolverr helper ───────────────────────────────────────────────────────
def flare_get(url: str, retries: int = 3) -> str | None:
    for attempt in range(retries):
        try:
            r = requests.post(
                FLARESOLVERR,
                json={"cmd": "request.get", "url": url, "maxTimeout": 60000},
                timeout=90,
            )
            d = r.json()
            if d.get("status") == "ok":
                return d["solution"]["response"]
            print(f"  [warn] FlareSolverr: {d.get('message', 'unknown error')}")
        except Exception as exc:
            print(f"  [warn] attempt {attempt + 1} – {exc}")
        time.sleep(4 * (attempt + 1))
    print(f"  [error] gave up on {url}")
    return None


# ── article list ──────────────────────────────────────────────────────────────
def fetch_story_list(category_slug: str) -> list[dict]:
    stories: list[dict] = []
    for page in range(1, MAX_API_PAGES + 1):
        url  = f"{API_BASE}?category_slug={category_slug}&page={page}&page_size=15"
        raw  = flare_get(url)
        if not raw:
            break
        try:
            text = raw.strip()
            # FlareSolverr sometimes wraps JSON in a bare HTML shell
            if text.startswith("<"):
                soup = BeautifulSoup(text, "lxml")
                pre  = soup.find("pre")
                text = pre.get_text() if pre else soup.get_text()
            data    = json.loads(text)
            results = data.get("results", [])
            stories.extend(results)
            print(f"    page {page}: {len(results)} articles")
            if not data.get("next"):
                break
        except Exception as exc:
            print(f"  [error] list parse page {page}: {exc}")
            break
        time.sleep(2)
    return stories


# ── body block renderer ───────────────────────────────────────────────────────
def render_blocks(blocks: list) -> str:
    """Convert Wagtail StreamField blocks to an HTML string."""
    parts: list[str] = []
    for block in blocks:
        btype = block.get("type", "")
        val   = block.get("value", "")

        if btype in ("paragraph", "rich_text", "richtext", "body") and isinstance(val, str):
            parts.append(val)

        elif btype == "heading" and isinstance(val, str):
            parts.append(f"<h3>{html_mod.escape(val)}</h3>")

        elif btype in ("image", "blog_image") and isinstance(val, dict):
            src     = val.get("download_url") or val.get("url", "")
            caption = val.get("caption", "")
            if src:
                cap_html = (f"<figcaption>{html_mod.escape(caption)}</figcaption>"
                            if caption else "")
                parts.append(f'<figure><img src="{src}" style="max-width:100%">'
                              f'{cap_html}</figure>')

        elif btype == "embed" and isinstance(val, dict):
            href = val.get("url", "")
            if href:
                parts.append(f'<p><a href="{href}">{href}</a></p>')

        elif btype == "quote" and isinstance(val, (str, dict)):
            quote = val if isinstance(val, str) else val.get("quote", str(val))
            parts.append(f"<blockquote>{html_mod.escape(quote)}</blockquote>")

        elif isinstance(val, str) and val.strip():
            parts.append(f"<p>{html_mod.escape(val)}</p>")

    return "\n".join(parts)


# ── article content (with cache) ──────────────────────────────────────────────
def _find_body(obj, depth: int = 0):
    """Recursively hunt for a non-empty StreamField 'body' list."""
    if depth > 6:
        return None
    if isinstance(obj, dict):
        if isinstance(obj.get("body"), list) and obj["body"]:
            return obj["body"]
        for v in obj.values():
            found = _find_body(v, depth + 1)
            if found:
                return found
    elif isinstance(obj, list):
        for item in obj:
            found = _find_body(item, depth + 1)
            if found:
                return found
    return None


def _scrape_content(news_slug: str, category_slug: str) -> str:
    """Actually fetch and parse one article page via FlareSolverr."""
    url = f"{SITE_BASE}/{category_slug}/{news_slug}"
    raw = flare_get(url)
    if not raw:
        return ""

    soup = BeautifulSoup(raw, "lxml")

    # ── try __NEXT_DATA__ (structured, preferred) ─────────────────────────────
    nd_tag = soup.find("script", {"id": "__NEXT_DATA__"})
    if nd_tag and nd_tag.string:
        try:
            nd          = json.loads(nd_tag.string)
            page_props  = nd.get("props", {}).get("pageProps", {})
            body_blocks = _find_body(page_props)
            if body_blocks:
                rendered = render_blocks(body_blocks)
                if len(rendered) > 80:
                    return rendered
        except Exception as exc:
            print(f"    [warn] __NEXT_DATA__ parse: {exc}")

    # ── fallback: scrape visible HTML ─────────────────────────────────────────
    for sel in ("article", "[class*='article-body']", "[class*='story-body']",
                "[class*='content']", "main"):
        el = soup.select_one(sel)
        if el:
            for dead in el.select("nav,header,footer,script,style,[class*='ad']"):
                dead.decompose()
            text = el.get_text(" ", strip=True)
            if len(text) > 150:
                return f"<p>{html_mod.escape(text[:6000])}</p>"

    return ""


def get_article_content(news_slug: str, category_slug: str,
                        seen: dict) -> tuple[str, bool]:
    """
    Return (content_html, was_cached).
    If the slug is already in seen, return cached content immediately.
    Otherwise fetch via FlareSolverr, store in seen, and return fresh content.
    """
    if news_slug in seen:
        return seen[news_slug]["content"], True

    content = _scrape_content(news_slug, category_slug)
    seen[news_slug] = {
        "content":    content,
        "fetched_at": datetime.now(timezone.utc).isoformat(),
    }
    return content, False


# ── RSS builder ───────────────────────────────────────────────────────────────
def build_rss(articles: list[tuple[dict, str]], category_slug: str,
              title_bn: str, out_file: str) -> None:
    fg = FeedGenerator()
    fg.id(f"{SITE_BASE}/{category_slug}")
    fg.title(f"{title_bn} | চরচা")
    fg.link(href=f"{SITE_BASE}/{category_slug}", rel="alternate")
    fg.link(href=f"{SITE_BASE}/{category_slug}.xml", rel="self")
    fg.language("bn")
    fg.description(f"চরচা – {title_bn}")

    for story, content in articles:
        slug = story.get("news_slug", "")
        if not slug:
            continue

        title    = story.get("title", "(শিরোনাম নেই)")
        url      = f"{SITE_BASE}/{category_slug}/{slug}"
        excerpt  = story.get("excerpt", "")
        pub_str  = (story.get("meta") or {}).get("first_published_at", "")
        img_info = story.get("blog_image") or {}
        thumb    = img_info.get("download_url", "")
        caption  = img_info.get("caption", "")

        pub_dt = datetime.now(timezone.utc)
        if pub_str:
            try:
                pub_dt = datetime.fromisoformat(pub_str.replace("Z", "+00:00"))
            except Exception:
                pass

        desc_parts: list[str] = []
        if thumb:
            alt = html_mod.escape(title)
            cap = (f"<br><small>{html_mod.escape(caption)}</small>" if caption else "")
            desc_parts.append(f'<p><img src="{thumb}" alt="{alt}" style="max-width:100%">{cap}</p>')
        if content:
            desc_parts.append(content)
        elif excerpt:
            desc_parts.append(f"<p>{html_mod.escape(excerpt)}</p>")

        fe = fg.add_entry()
        fe.id(url)
        fe.title(title)
        fe.link(href=url)
        fe.published(pub_dt)
        fe.updated(pub_dt)
        fe.description("\n".join(desc_parts))

    fg.rss_file(out_file, pretty=True)
    print(f"  → saved {out_file}")


# ── index.html ────────────────────────────────────────────────────────────────
def write_index(done: list[str], seen: dict) -> None:
    now        = datetime.now(timezone.utc).strftime("%Y-%m-%d %H:%M UTC")
    total_seen = len(seen)

    cards = ""
    for slug, title_bn in CATEGORIES:
        if slug not in done:
            continue
        cards += f"""
    <div class="card">
      <div class="card-icon">📰</div>
      <h2>{title_bn}</h2>
      <p class="src"><a href="{SITE_BASE}/{slug}" target="_blank">{SITE_BASE}/{slug}</a></p>
      <div class="btns">
        <a class="btn-rss" href="{slug}.xml">📡 RSS</a>
        <a class="btn-web" href="{SITE_BASE}/{slug}" target="_blank">🌐 Source</a>
      </div>
    </div>"""

    html_out = f"""<!DOCTYPE html>
<html lang="bn">
<head>
<meta charset="UTF-8">
<meta name="viewport" content="width=device-width,initial-scale=1">
<title>Chaarcha RSS Feeds</title>
<style>
  :root{{--blue:#274e8f;--light:#f0f4fb;}}
  *{{box-sizing:border-box;margin:0;padding:0}}
  body{{font-family:-apple-system,BlinkMacSystemFont,'Segoe UI',sans-serif;
        background:var(--light);color:#222;padding:2rem}}
  header{{max-width:860px;margin:0 auto 2rem}}
  h1{{color:var(--blue);font-size:1.9rem;margin-bottom:.3rem}}
  .meta{{color:#666;font-size:.85rem}}
  .cache-note{{margin-top:.4rem;font-size:.8rem;color:#888;
               background:#fff;display:inline-block;padding:.2rem .7rem;
               border-radius:20px;border:1px solid #dde}}
  .grid{{display:grid;grid-template-columns:repeat(auto-fit,minmax(240px,1fr));
         gap:1.2rem;max-width:860px;margin:0 auto}}
  .card{{background:#fff;border-radius:12px;padding:1.5rem;
         box-shadow:0 2px 10px rgba(0,0,0,.07);display:flex;flex-direction:column;gap:.6rem}}
  .card-icon{{font-size:2rem}}
  .card h2{{color:var(--blue);font-size:1.15rem}}
  .src{{font-size:.72rem;color:#999;word-break:break-all}}
  .src a{{color:#999;text-decoration:none}}
  .btns{{display:flex;gap:.6rem;margin-top:.4rem}}
  .btn-rss,.btn-web{{padding:.4rem .9rem;border-radius:7px;
                     text-decoration:none;font-size:.88rem;font-weight:600}}
  .btn-rss{{background:var(--blue);color:#fff}}
  .btn-web{{background:#e8eef7;color:var(--blue)}}
  .btn-rss:hover{{background:#1a3a6b}}
  .btn-web:hover{{background:#ccd8ee}}
  footer{{text-align:center;margin-top:2.5rem;font-size:.78rem;color:#aaa}}
  footer a{{color:#aaa}}
</style>
</head>
<body>
<header>
  <h1>চরচা RSS Feeds</h1>
  <p class="meta">Last updated: {now} &nbsp;·&nbsp; refreshes every 4 hours</p>
  <p class="cache-note">📦 {total_seen} articles cached in seen.json</p>
</header>
<div class="grid">{cards}
</div>
<footer>
  Scraped via FlareSolverr &nbsp;·&nbsp;
  <a href="https://www.chaarcha.com" target="_blank">chaarcha.com</a>
</footer>
</body>
</html>"""
    with open("index.html", "w", encoding="utf-8") as f:
        f.write(html_out)
    print("  → saved index.html")


# ── main ──────────────────────────────────────────────────────────────────────
def main() -> None:
    seen = load_seen()
    print(f"[cache] {len(seen)} slugs already in {SEEN_FILE}")

    done: list[str] = []

    for category_slug, title_bn in CATEGORIES:
        print(f"\n[{category_slug}] fetching list …")
        stories = fetch_story_list(category_slug)
        print(f"  found {len(stories)} articles total, using up to {MAX_ARTICLES}")

        articles:  list[tuple[dict, str]] = []
        new_count  = 0
        hit_count  = 0

        for i, story in enumerate(stories[:MAX_ARTICLES]):
            slug = story.get("news_slug", "")
            if not slug:
                continue

            short   = story.get("title", slug)[:65]
            content, cached = get_article_content(slug, category_slug, seen)

            if cached:
                hit_count += 1
                print(f"  [{i+1:02d}] (cache) {short}")
            else:
                new_count += 1
                print(f"  [{i+1:02d}] (fetch) {short}")
                # only sleep after a real network request
                time.sleep(2)

            articles.append((story, content))

        print(f"  summary: {hit_count} cached  {new_count} newly fetched")
        build_rss(articles, category_slug, title_bn, f"{category_slug}.xml")
        done.append(category_slug)

        # save after each category so a mid-run crash doesn't lose work
        save_seen(seen)
        time.sleep(1)

    write_index(done, seen)
    save_seen(seen)   # final save
    print(f"\n✓ done. seen.json now has {len(seen)} entries.")


if __name__ == "__main__":
    main()

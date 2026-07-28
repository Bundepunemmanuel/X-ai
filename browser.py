"""
browser.py — Playwright automation, read-only by design.

We deliberately do NOT attempt to log into X from the server anymore. X's
anti-automation system (Arkose) reliably blocks server-side logins and cookie
replay from datacenter IPs — confirmed by both our own testing and outside
sources on this exact failure mode. Fighting that further has diminishing
returns, so this file only does what works reliably logged-out:
  - searching X for candidate threads
  - reading a thread's post + replies
  - reading an arbitrary external URL (for chat / commenter-link context)
  - a general web search fallback (DuckDuckGo)

Actually posting/replying/liking happens on the OPERATOR'S OWN PHONE via
X's "intent" links (see build_reply_intent_url / build_like_intent_url /
build_post_intent_url in agent.py) — the dashboard opens these for you to
tap "Post" yourself, using your own already-logged-in session. That's the
only step that ever needed a real login, so that's the only step we hand off.
"""

import time
import random

from playwright.sync_api import sync_playwright

import config

BASE_URL = "https://x.com"

DEBUG_SCREENSHOT_PATH = "/tmp/debug_last_failure.png"


def _save_debug_screenshot(page, label: str):
    try:
        page.screenshot(path=DEBUG_SCREENSHOT_PATH)
        print(f"[browser] saved debug screenshot for '{label}' — view at /api/debug/screenshot")
    except Exception as e:
        print(f"[browser] could not save debug screenshot: {e}")


class XBrowser:
    """Wraps a single persistent Playwright browser context, used read-only."""

    def __init__(self):
        self._playwright = None
        self._browser = None
        self._context = None
        self._page = None

    def start(self):
        self._playwright = sync_playwright().start()
        self._browser = self._playwright.chromium.launch(
            headless=config.HEADLESS,
            args=[
                "--disable-blink-features=AutomationControlled",
                "--disable-dev-shm-usage",
                "--no-sandbox",
            ],
        )
        self._context = self._browser.new_context(
            user_agent=(
                "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 "
                "(KHTML, like Gecko) Chrome/128.0.0.0 Safari/537.36"
            ),
            viewport={"width": 1280, "height": 800},
            locale="en-US",
            timezone_id="America/New_York",
        )
        self._context.add_init_script(
            "Object.defineProperty(navigator, 'webdriver', { get: () => undefined });"
        )
        self._page = self._context.new_page()
        print("[browser] started (read-only mode — no login attempted)")

    def stop(self):
        if self._browser:
            self._browser.close()
        if self._playwright:
            self._playwright.stop()

    # ─── Scanning for candidate threads (works logged out) ──────────────
    def search_recent_posts(self, query: str, max_results: int = 5):
        page = self._page
        search_url = f"{BASE_URL}/search?q={query.replace(' ', '%20')}&src=typed_query&f=live"
        try:
            page.goto(search_url, wait_until="domcontentloaded", timeout=20000)
        except Exception as e:
            print(f"[browser] search navigation failed: {e}")
            return []
        time.sleep(3)

        results = []
        seen_urls = set()
        articles = page.locator('article[data-testid="tweet"]')
        count = min(articles.count(), max_results)

        for i in range(count):
            try:
                article = articles.nth(i)
                text_el = article.locator('[data-testid="tweetText"]').first
                text = text_el.inner_text() if text_el.count() else ""

                link_el = article.locator('a[href*="/status/"]').first
                href = link_el.get_attribute("href") if link_el.count() else None
                if not href:
                    continue
                url = f"{BASE_URL}{href}" if href.startswith("/") else href
                if url in seen_urls:
                    continue
                seen_urls.add(url)

                handle_el = article.locator('[data-testid="User-Name"] a').first
                handle = handle_el.get_attribute("href") if handle_el.count() else ""
                handle = handle.strip("/").split("/")[-1] if handle else "unknown"

                name_el = article.locator('[data-testid="User-Name"] span').first
                name = name_el.inner_text() if name_el.count() else handle

                results.append({"url": url, "handle": f"@{handle}", "name": name, "text": text})
            except Exception as e:
                print(f"[browser] error parsing search result {i}: {e}")
                continue

        return results

    # ─── Reading a full thread (post + replies), works logged out ───────
    def read_thread(self, thread_url: str):
        page = self._page
        try:
            page.goto(thread_url, wait_until="domcontentloaded", timeout=20000)
        except Exception as e:
            print(f"[browser] could not load thread {thread_url}: {e}")
            return {"post_text": "", "replies": []}
        time.sleep(2)

        articles = page.locator('article[data-testid="tweet"]')
        count = articles.count()
        if count == 0:
            return {"post_text": "", "replies": []}

        post_text_el = articles.nth(0).locator('[data-testid="tweetText"]').first
        post_text = post_text_el.inner_text() if post_text_el.count() else ""

        replies = []
        for i in range(1, min(count, 8)):
            try:
                reply_text_el = articles.nth(i).locator('[data-testid="tweetText"]').first
                if reply_text_el.count():
                    replies.append(reply_text_el.inner_text())
            except Exception:
                continue

        return {"post_text": post_text, "replies": replies}

    # ─── Reading an arbitrary external link (chat: "check this URL") ────
    def read_external_link(self, url: str, max_chars: int = 1500) -> str:
        try:
            new_page = self._context.new_page()
            new_page.goto(url, wait_until="domcontentloaded", timeout=25000)
            time.sleep(1.5)
            text = new_page.locator("body").inner_text()
            new_page.close()
            return text[:max_chars]
        except Exception as e:
            print(f"[browser] could not read external link {url}: {e}")
            return ""

    # ─── General web search (chat: "search X for Y") ────────────────────
    def web_search(self, query: str, max_results: int = 5):
        try:
            new_page = self._context.new_page()
            search_url = f"https://html.duckduckgo.com/html/?q={query.replace(' ', '+')}"
            new_page.goto(search_url, wait_until="domcontentloaded", timeout=15000)
            time.sleep(1.5)

            results = []
            result_blocks = new_page.locator(".result")
            count = min(result_blocks.count(), max_results)
            for i in range(count):
                block = result_blocks.nth(i)
                title_el = block.locator(".result__title").first
                snippet_el = block.locator(".result__snippet").first
                link_el = block.locator(".result__url").first
                title = title_el.inner_text() if title_el.count() else ""
                snippet = snippet_el.inner_text() if snippet_el.count() else ""
                url = link_el.inner_text() if link_el.count() else ""
                if title:
                    results.append({"title": title, "snippet": snippet, "url": url})

            new_page.close()
            return results
        except Exception as e:
            print(f"[browser] web search failed for '{query}': {e}")
            return []


def jittered_delay():
    """Random delay between actions, weighted toward the higher end."""
    lo, hi = config.MIN_GAP_SECONDS, config.MAX_GAP_SECONDS
    delay = random.triangular(lo, hi, hi)
    time.sleep(delay)

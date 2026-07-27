"""
browser.py — the only file that touches Playwright / the actual browser.
Everything here operates on a live X (twitter.com) session. Nothing here calls
Gemini or the database directly — it just returns plain data (dicts/strings)
for agent.py to act on.

NOTE ON SELECTORS: X's DOM changes periodically. The data-testid attributes
used below (tweet, tweetText, reply, etc.) have been stable identifiers for
X's web client for a long time, but if X ships a redesign, these may need
updating — check with browser dev tools if scanning/posting starts failing.
"""

import time
import random

from playwright.sync_api import sync_playwright

import config

BASE_URL = "https://x.com"


class XBrowser:
    """Wraps a single persistent Playwright browser context for the assistant account."""

    def __init__(self):
        self._playwright = None
        self._browser = None
        self._context = None
        self._page = None
        self.logged_in = False
        self.last_error = None

    def start(self):
        self._playwright = sync_playwright().start()
        self._browser = self._playwright.chromium.launch(headless=config.HEADLESS)
        try:
            self._context = self._browser.new_context(storage_state=config.SESSION_STATE_PATH)
            print("[browser] reusing saved session")
        except Exception:
            self._context = self._browser.new_context()
            print("[browser] no saved session found, starting fresh")
        self._page = self._context.new_page()
        # NOTE: login is intentionally NOT attempted here. The browser is usable
        # for general page-reading (e.g. chat asking it to look at a URL) as soon
        # as it starts, independent of whether X credentials are configured or
        # whether login succeeds. Call try_login() separately for X-specific work.
        self.logged_in = False

    def try_login(self) -> bool:
        """Attempts X login if credentials are configured. Never raises — returns
        True/False and logs the outcome, so a missing/failing login doesn't take
        down the rest of the browser's usefulness (e.g. reading external URLs)."""
        if not config.X_USERNAME or not config.X_PASSWORD:
            print("[browser] X_USERNAME/X_PASSWORD not set — skipping X login. "
                  "General page-reading still works, but scanning/posting to X won't until these are set.")
            self.logged_in = False
            return False
        try:
            self._ensure_logged_in()
            self.logged_in = True
            return True
        except Exception as e:
            print(f"[browser] X login failed: {e}")
            self.logged_in = False
            return False

    def stop(self):
        if self._context:
            self._context.storage_state(path=config.SESSION_STATE_PATH)
        if self._browser:
            self._browser.close()
        if self._playwright:
            self._playwright.stop()

    # ─── Login / session ────────────────────────────────────────────────
    def _is_logged_in(self) -> bool:
        self._page.goto(f"{BASE_URL}/home", wait_until="domcontentloaded")
        time.sleep(2)
        return "login" not in self._page.url and "flow" not in self._page.url

    def _ensure_logged_in(self):
        if self._is_logged_in():
            print("[browser] session already valid, skipping login")
            return

        print("[browser] logging in fresh")
        page = self._page
        page.goto(f"{BASE_URL}/i/flow/login", wait_until="domcontentloaded")
        time.sleep(2)

        # username step
        page.fill('input[autocomplete="username"]', config.X_USERNAME)
        page.keyboard.press("Enter")
        time.sleep(2)

        # X sometimes asks for a second identity check (phone/email) before password
        # if it thinks the login looks unusual — handle the common case where it doesn't.
        try:
            page.wait_for_selector('input[name="password"]', timeout=5000)
        except Exception:
            print("[browser] unexpected extra verification step — manual login may be required")
            raise RuntimeError("X login requires manual verification step (unexpected checkpoint)")

        page.fill('input[name="password"]', config.X_PASSWORD)
        page.keyboard.press("Enter")
        time.sleep(3)

        if not self._is_logged_in():
            raise RuntimeError(
                "X login failed — check credentials, or a 2FA/verification step is blocking automated login"
            )

        self._context.storage_state(path=config.SESSION_STATE_PATH)
        print("[browser] login successful, session saved")

    # ─── Scanning for candidate threads ─────────────────────────────────
    def search_recent_posts(self, query: str, max_results: int = 15):
        """Searches X for recent posts matching a query. Returns list of {url, handle, name, text}."""
        page = self._page
        search_url = f"{BASE_URL}/search?q={query.replace(' ', '%20')}&src=typed_query&f=live"
        page.goto(search_url, wait_until="domcontentloaded")
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

    # ─── Reading a full thread (post + replies) ─────────────────────────
    def read_thread(self, thread_url: str):
        """Returns {"post_text": str, "replies": [str, ...]}"""
        page = self._page
        page.goto(thread_url, wait_until="domcontentloaded")
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

    # ─── Opening a commenter's linked product page ──────────────────────
    def read_external_link(self, url: str, max_chars: int = 1500) -> str:
        """Opens an external URL in a new tab and grabs visible text content. Best-effort."""
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

    # ─── General web search (for chat: "search X for Y" style requests) ──
    def web_search(self, query: str, max_results: int = 5):
        """Runs a search on DuckDuckGo's HTML endpoint (no JS required, doesn't need
        login, and is much more scrape-friendly than Google). Returns list of
        {title, snippet, url}."""
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

    # ─── Liking a post (simplest possible way to verify login works for real) ─
    def like_post(self, thread_url: str) -> bool:
        page = self._page
        try:
            page.goto(thread_url, wait_until="domcontentloaded")
            time.sleep(2)
            like_button = page.locator('[data-testid="like"]').first
            like_button.wait_for(state="visible", timeout=15000)
            like_button.click()
            time.sleep(1.5)
            print(f"[browser] liked {thread_url}")
            return True
        except Exception as e:
            print(f"[browser] failed to like {thread_url}: {e}")
            self.last_error = f"Failed to like post: {e}"
            return False

    # ─── Posting a reply ─────────────────────────────────────────────────
    def post_reply(self, thread_url: str, reply_text: str) -> bool:
        page = self._page
        try:
            page.goto(thread_url, wait_until="domcontentloaded")
            time.sleep(2)

            reply_box = page.locator('[data-testid="tweetTextarea_0"]').first
            reply_box.click()
            reply_box.fill(reply_text)
            time.sleep(1)

            send_button = page.locator('[data-testid="tweetButton"]').first
            send_button.click()
            time.sleep(2)
            return True
        except Exception as e:
            print(f"[browser] failed to post reply to {thread_url}: {e}")
            return False

    # ─── Posting an original post ────────────────────────────────────────
    def post_original(self, text: str) -> bool:
        page = self._page

        # primary path: dedicated compose URL
        try:
            page.goto(f"{BASE_URL}/compose/post", wait_until="domcontentloaded")
            box = page.locator('[data-testid="tweetTextarea_0"]').first
            box.wait_for(state="visible", timeout=15000)
            box.click()
            box.fill(text)
            time.sleep(1)
            page.locator('[data-testid="tweetButton"]').first.click()
            time.sleep(2)
            print("[browser] posted original via /compose/post")
            return True
        except Exception as e:
            print(f"[browser] /compose/post path failed ({e}), trying home feed compose button fallback")

        # fallback: go to home feed and click the "New post" button to open the composer,
        # since X's dedicated compose URL doesn't always render reliably
        try:
            page.goto(f"{BASE_URL}/home", wait_until="domcontentloaded")
            time.sleep(2)
            new_post_button = page.locator('[data-testid="SideNav_NewTweet_Button"]').first
            new_post_button.wait_for(state="visible", timeout=15000)
            new_post_button.click()
            time.sleep(1.5)

            box = page.locator('[data-testid="tweetTextarea_0"]').first
            box.wait_for(state="visible", timeout=15000)
            box.click()
            box.fill(text)
            time.sleep(1)
            page.locator('[data-testid="tweetButton"]').first.click()
            time.sleep(2)
            print("[browser] posted original via home feed compose button")
            return True
        except Exception as e:
            print(f"[browser] home feed compose fallback also failed: {e}")
            self.last_error = f"Both compose paths failed to post original post: {e}"
            return False

    # ─── Notifications / mentions (replies to the assistant's own posts) ─
    def get_new_mentions(self, max_results: int = 15):
        """Returns list of {url, handle, name, text} for recent replies to the assistant account."""
        page = self._page
        page.goto(f"{BASE_URL}/notifications/mentions", wait_until="domcontentloaded")
        time.sleep(3)

        results = []
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
                handle_el = article.locator('[data-testid="User-Name"] a').first
                handle = handle_el.get_attribute("href") if handle_el.count() else ""
                handle = handle.strip("/").split("/")[-1] if handle else "unknown"
                name_el = article.locator('[data-testid="User-Name"] span').first
                name = name_el.inner_text() if name_el.count() else handle
                results.append({"url": url, "handle": f"@{handle}", "name": name, "text": text})
            except Exception:
                continue
        return results

    # ─── Reactive DMs ─────────────────────────────────────────────────────
    def get_new_dm_conversations(self, max_results: int = 10):
        """Returns list of {conversation_url, handle, last_message} for DMs where the other
        person sent the most recent message (i.e. reactive — they messaged first/most recently)."""
        page = self._page
        page.goto(f"{BASE_URL}/messages", wait_until="domcontentloaded")
        time.sleep(3)

        results = []
        conversations = page.locator('[data-testid="conversation"]')
        count = min(conversations.count(), max_results)
        for i in range(count):
            try:
                convo = conversations.nth(i)
                # X bolds/highlights unread conversations — this is a best-effort check
                unread = convo.locator('[data-testid="socialContext"]').count() > 0
                if not unread:
                    continue
                convo.click()
                time.sleep(2)
                last_msg_el = page.locator('[data-testid="messageEntry"]').last
                last_message = last_msg_el.inner_text() if last_msg_el.count() else ""
                results.append({
                    "conversation_url": page.url,
                    "last_message": last_message,
                })
            except Exception:
                continue
        return results

    def send_dm(self, conversation_url: str, text: str) -> bool:
        page = self._page
        try:
            page.goto(conversation_url, wait_until="domcontentloaded")
            time.sleep(2)
            box = page.locator('[data-testid="dmComposerTextInput"]').first
            box.click()
            box.fill(text)
            time.sleep(1)
            page.locator('[data-testid="dmComposerSendButton"]').first.click()
            time.sleep(1)
            return True
        except Exception as e:
            print(f"[browser] failed to send DM: {e}")
            return False


def jittered_delay():
    """Random delay between actions, weighted toward the higher end — avoids
    a metronome-even reply cadence that reads as automated."""
    lo, hi = config.MIN_GAP_SECONDS, config.MAX_GAP_SECONDS
    # weight toward higher end using a simple triangular distribution
    delay = random.triangular(lo, hi, hi)
    time.sleep(delay)

"""Twitter/X MCP server (stdio). Drives ONE persistent Firefox window (login saved in ./x_profile_ff).
READ: read_search, read_profile, read_home, read_post.  WRITE: like_post, reply_to_post, create_post
(each needs confirm=true, is capped per day, and is paced).  Also: get_tweet (no login), queue_*.
Unofficial browser automation; X may limit or suspend accounts."""
import os, json, asyncio, random, pathlib, datetime, contextlib, re, urllib.request, urllib.parse
from mcp.server.mcpserver import MCPServer
from playwright.async_api import async_playwright

HERE = pathlib.Path(__file__).parent
DATA = pathlib.Path(os.environ.get("X_MCP_HOME", pathlib.Path.home() / ".twitter-mcp")).expanduser()
DATA.mkdir(parents=True, exist_ok=True)
PROFILE = os.environ.get("X_PROFILE", str(DATA / "firefox-profile"))
QUEUE = DATA / "queue.json"
LOG = DATA / "actions-log.json"
DAILY_CAP = 30  # ponytail: likes+replies+posts per day; raise at your own ban risk
TABS = {"top": "", "latest": "&f=live", "media": "&f=media", "people": "&f=user"}
mcp = MCPServer("twitter")
lock = asyncio.Lock()
_pw = _ctx = _page = _me = None

# X dropped data-testid="tweet" from the <article>; use the <article> itself and parse inside it.
POSTS_JS = """() => [...document.querySelectorAll('article')].map(a => {
  const link = [...a.querySelectorAll('a[href*="/status/"]')].find(x => x.querySelector('time'));
  const txt = a.querySelector('[data-testid="tweetText"]');
  const g = a.querySelector('div[role="group"]');
  return {
    url: link ? link.href.split('?')[0] : null,
    author: (a.querySelector('[data-testid="User-Name"]') || {}).innerText || '',
    text: txt ? txt.innerText : '',
    time: link && link.querySelector('time') ? link.querySelector('time').dateTime : null,
    photos: [...a.querySelectorAll('img[src*="twimg.com/media"]')].map(i => i.src),
    videos: [...a.querySelectorAll('video')].map(v => v.poster || v.src),
    stats: g ? g.getAttribute('aria-label') : null,
    liked: !!a.querySelector('[data-testid="unlike"]'),
  };
}).filter(t => t.url)"""
PEOPLE_JS = """() => [...document.querySelectorAll('[data-testid="UserCell"]')].map(c => ({
  profile: (c.querySelector('a[href^="/"]') || {}).href || '', text: c.innerText}))"""
# Media tab is a grid of photo tiles (<a> wrapping an <img>), not <article> elements.
MEDIA_JS = """() => [...document.querySelectorAll('a[href*="/status/"]')]
  .filter(a => a.querySelector('img[src*="twimg.com/media"], img[src*="ext_tw_video_thumb"]'))
  .map(a => ({url: a.href.split('?')[0].replace(/\\/(photo|video)\\/\\d+$/, ''),
              media: [a.querySelector('img').src]}))"""

async def _launch_ctx():
    """Launch the persistent context, retrying through the window where a just-crashed or
    sleep-killed Firefox still holds the profile. Also restarts the Playwright driver if its
    pipe died (what 'I/O operation on closed pipe' after a sleep means)."""
    global _pw
    last = None
    for attempt in range(4):
        if _pw is None:
            _pw = await async_playwright().start()
        try:
            return await _pw.firefox.launch_persistent_context(
                PROFILE, headless=os.environ.get("X_HEADLESS") == "1",  # headless is far more stable
                timeout=60000, color_scheme="dark")  # color_scheme: report device as dark mode
        except Exception as e:
            last = e
            with contextlib.suppress(Exception):
                await _pw.stop()
            _pw = None  # force a fresh driver next attempt (handles a dead driver pipe)
            await asyncio.sleep(3 * (attempt + 1))  # 3,6,9s: let the dying process release the profile
    raise last

@contextlib.asynccontextmanager
async def tab():
    """Yield the single reused tab of the long-lived, logged-in browser (one window, serialized)."""
    global _pw, _ctx, _page
    async with lock:
        live = False
        if _ctx is not None and _page is not None:
            with contextlib.suppress(Exception):
                live = not _page.is_closed()
        if not live:
            _ctx = None
            _ctx = await _launch_ctx()
            _ctx.set_default_timeout(20000)  # fail fast instead of hanging 30s
            existing = await _ctx.cookies(["https://x.com", "https://twitter.com"])
            backup = DATA / "cookies.json"
            if not any(c.get("name") == "auth_token" and c.get("value") for c in existing) and backup.exists():
                from login import load_cookies
                await _ctx.add_cookies(load_cookies(backup))
            _page = _ctx.pages[0] if _ctx.pages else await _ctx.new_page()  # reuse the blank start page
        yield _page

async def open_x(page, url):
    parsed = urllib.parse.urlsplit(url)
    if parsed.scheme != "https" or parsed.hostname not in ("x.com", "twitter.com", "www.x.com", "www.twitter.com") or parsed.username or parsed.password or parsed.port not in (None, 443):
        raise ValueError("Only HTTPS X/Twitter URLs are allowed")
    # X holds streaming connections open, so the "load" event often never fires -> goto would hang
    # the full timeout every time. domcontentloaded fires once the HTML+SPA shell is ready.
    await page.goto(url, wait_until="domcontentloaded", timeout=25000)
    await page.wait_for_timeout(3500)
    if "/login" in page.url or "/i/flow" in page.url or await page.locator('[data-testid="loginButton"]').count():
        raise RuntimeError("Not logged in to X in this profile. Close the MCP browser and run login.py once.")

def compact(p):
    """Drop empty fields and the author cruft so the agent spends tokens only on real content."""
    lines = [x for x in (p.get("author") or "").split("\n") if x.strip()]
    handle = next((x for x in lines if x.startswith("@")), "")
    out = {"url": p["url"], "text": (p.get("text") or "").strip()}
    if lines:
        out["name"] = lines[0]
    if handle:
        out["handle"] = handle
    if p.get("time"):
        out["time"] = p["time"]
    media = (p.get("photos") or []) + (p.get("videos") or [])
    if media:
        out["media"] = media
    if p.get("stats"):
        out["stats"] = p["stats"]
    if p.get("liked"):
        out["liked"] = True
    return out

async def collect(page, count, js=POSTS_JS, comp=compact):
    """Scroll and gather up to `count` unique items. js/comp let the media grid use its own shape."""
    count = max(1, min(count, 100))
    seen, prev, stale = {}, -1, 0
    for _ in range(40):
        for t in await page.evaluate(js):
            if t["url"] and t["url"] not in seen:
                seen[t["url"]] = t
        if len(seen) >= count:
            break
        stale = stale + 1 if len(seen) == prev else 0
        if stale >= 4:
            break
        prev = len(seen)
        await page.mouse.wheel(0, 2500)
        await asyncio.sleep(random.uniform(1.5, 3.5))  # ponytail: fixed pacing, add backoff on 429
    return [comp(p) for p in list(seen.values())[:count]]

async def _find_url(page, marker):
    """URL of the visible post whose text contains `marker` (to return a reversible handle)."""
    for p in await page.evaluate(POSTS_JS):
        if marker in (p.get("text") or "") and p["url"]:
            return p["url"]
    return None

@mcp.tool()
async def read_search(query: str, tab: str = "top", count: int = 10) -> list[dict]:
    """Search X. tab: top | latest | media | people. Returns up to `count` posts with text, author,
    time, photo URLs, video thumbnails, stats and whether you already liked it. (people returns profiles.)"""
    if tab not in TABS:
        raise ValueError(f"tab must be one of {list(TABS)}")
    async with tab_ctx() as page:
        await open_x(page, f"https://x.com/search?q={urllib.parse.quote(query)}{TABS[tab]}")
        if tab == "people":
            return (await page.evaluate(PEOPLE_JS))[:max(1, min(count, 100))]
        if tab == "media":
            return await collect(page, count, MEDIA_JS, lambda p: p)
        return await collect(page, count)

@mcp.tool()
async def read_profile(username: str, count: int = 10) -> list[dict]:
    """Recent posts on a profile, with media."""
    async with tab_ctx() as page:
        await open_x(page, f"https://x.com/{username.lstrip('@')}")
        return await collect(page, count)

@mcp.tool()
async def read_home(count: int = 10, feed: str = "for_you") -> list[dict]:
    """Scroll your home feed. feed: for_you | following."""
    async with tab_ctx() as page:
        await open_x(page, "https://x.com/home")
        if feed == "following":
            # best-effort: a missed tab click must not hang the read; worst case you get For You
            with contextlib.suppress(Exception):
                await page.get_by_role("tab", name="Following").click(timeout=8000)
                await page.wait_for_timeout(2500)
        return await collect(page, count)

@mcp.tool()
async def read_post(url: str, count: int = 10) -> list[dict]:
    """One post plus its first replies (count includes the post itself)."""
    async with tab_ctx() as page:
        await open_x(page, url)
        return await collect(page, count)

tab_ctx = tab  # alias used above

_STAT_KEYS = {"reply": "replies", "replies": "replies", "repost": "reposts", "reposts": "reposts",
              "like": "likes", "likes": "likes", "bookmark": "bookmarks", "bookmarks": "bookmarks",
              "view": "views", "views": "views"}


def parse_count(s: str) -> int:
    """'1,234' -> 1234, '1.2K' -> 1200, '3M' -> 3000000, '' -> 0."""
    s = (s or "").strip().replace(",", "").upper()
    m = re.match(r"^([\d.]+)\s*([KMB]?)", s)
    if not m:
        return 0
    return int(round(float(m.group(1)) * {"": 1, "K": 1e3, "M": 1e6, "B": 1e9}[m.group(2)]))


def parse_stats(label) -> dict:
    """Stats aria-label ('12 replies, 3 reposts, 40 likes, 1 bookmark, 2345 views') -> counts."""
    out = {"replies": 0, "reposts": 0, "likes": 0, "bookmarks": 0, "views": 0}
    for num, word in re.findall(r"([\d,.]+[KMB]?)\s+([a-zA-Z]+)", label or ""):
        key = _STAT_KEYS.get(word.lower())
        if key:
            out[key] = parse_count(num)
    return out


@mcp.tool()
async def follower_count(username: str) -> int:
    """Follower count of a profile (0 if not visible)."""
    async with tab_ctx() as page:
        await open_x(page, f"https://x.com/{username.lstrip('@')}")
        link = page.locator('a[href$="/verified_followers"], a[href$="/followers"]').first
        if not await link.count():
            return 0
        return parse_count((await link.inner_text()).split()[0])

# ---------- write actions: gated, capped, paced ----------
def _used():
    d = datetime.date.today().isoformat()
    return d, (json.loads(LOG.read_text()).get(d, 0) if LOG.exists() else 0)

def gate(confirm):
    if not confirm:
        raise ValueError("Set confirm=true only when the user explicitly asked for this exact action in chat.")
    if _used()[1] >= DAILY_CAP:
        raise RuntimeError(f"Daily cap of {DAILY_CAP} write actions reached.")

def bump():
    d, n = _used()
    LOG.write_text(json.dumps({d: n + 1}))

async def pace():
    await asyncio.sleep(random.uniform(3, 8))

async def _reset():
    """Drop the browser so the next tab() relaunches it (used after a page crash)."""
    global _ctx, _page
    async with lock:
        if _ctx is not None:
            with contextlib.suppress(Exception):
                await _ctx.close()
        _ctx = _page = None

FLAKY = ("crash", "timeout", "closed", "intercept", "detached", "navigating")

async def _retry1(fn):
    """Run an idempotent browser action; on a flaky browser error reset once and retry."""
    try:
        return await fn()
    except Exception as e:
        if any(w in str(e).lower() for w in FLAKY):
            await _reset()
            await asyncio.sleep(2)
            return await fn()
        raise

async def _click(loc, timeout=8000):
    """Click robustly. X re-renders/overlays its controls (plain click hangs 'waiting for stable'),
    and a headful window that's occluded/unfocused never receives synthetic pointer input (even a
    FORCE click hangs). So: try a real click, then force, then dispatch the click in-page -- the last
    fires X's React handler directly and doesn't need the window focused or visible."""
    try:
        await loc.click(timeout=timeout)
        return
    except Exception:
        pass
    with contextlib.suppress(Exception):
        await loc.scroll_into_view_if_needed(timeout=3000)
    try:
        await loc.click(force=True, timeout=5000)
        return
    except Exception:
        pass
    await loc.dispatch_event("click")  # in-page event: immune to window occlusion/focus

@mcp.tool()
async def like_post(url: str, confirm: bool = False) -> str:
    """Like one post by URL (reverse with unlike_post). Only on explicit request; X bans bulk liking."""
    gate(confirm)
    async def do():
        async with tab_ctx() as page:
            await open_x(page, url)
            art = page.locator("article").first
            if await art.locator('[data-testid="unlike"]').count():
                return "already liked"
            await pace()
            await _click(art.locator('[data-testid="like"]').first)
            await page.wait_for_timeout(1500)
            ok = await art.locator('[data-testid="unlike"]').count() > 0
            if ok: bump()
            return "liked" if ok else "click sent but like not confirmed"
    return await _retry1(do)

@mcp.tool()
async def unlike_post(url: str, confirm: bool = False) -> str:
    """Remove your like from a post (reverses like_post). confirm=true required."""
    if not confirm:
        raise ValueError("Set confirm=true to unlike.")
    async def do():
        async with tab_ctx() as page:
            await open_x(page, url)
            art = page.locator("article").first
            if not await art.locator('[data-testid="unlike"]').count():
                return "not liked"
            await _click(art.locator('[data-testid="unlike"]').first)
            await page.wait_for_timeout(1500)
            return "unliked" if await art.locator('[data-testid="like"]').count() else "click sent but not confirmed"
    return await _retry1(do)

async def _submit(page, text, button):
    box = page.locator('[data-testid="tweetTextarea_0"]').first
    await box.wait_for(state="visible", timeout=15000)
    try:
        await box.click(timeout=7000)
    except Exception:
        await box.focus()  # overlay intercepted the click; focus directly
    await page.keyboard.type(text, delay=random.randint(40, 110))
    await pace()
    btn = page.locator(button).first
    await btn.wait_for(state="visible", timeout=10000)
    await _click(btn)
    await page.wait_for_timeout(3000)

async def _write(submit_url, button, text, locate_url):
    """Submit once and try to find the new post; an unknown outcome requires manual review."""
    marker = text.strip()[:40]
    async def submit():
        async with tab_ctx() as page:
            await open_x(page, submit_url)
            await _submit(page, text, button)
    async def locate():
        async with tab_ctx() as page:
            where = locate_url or f"https://x.com/{await _handle(page)}"
            for _ in range(6):
                await open_x(page, where)
                u = await _find_url(page, marker)
                if u:
                    return u
                await page.mouse.wheel(0, 800)
                await page.wait_for_timeout(2000)
        return None
    # A failed or delayed confirmation does not prove submission failed; never retry a post.
    try:
        await submit()
    except Exception as exc:
        raise RuntimeError("Submission outcome is uncertain. Check X before trying again.") from exc
    bump()
    try:
        return await locate()
    except Exception:
        return None

@mcp.tool()
async def reply_to_post(url: str, text: str, confirm: bool = False) -> dict:
    """Reply to a post by URL. Returns the reply's URL (reverse with delete_post). Only on explicit
    request. X only allows automated replies to people who engaged with you first, so use sparingly."""
    gate(confirm)
    if not text.strip():
        raise ValueError("empty reply")
    result = await _write(url, '[data-testid="tweetButtonInline"]', text, url)
    return {"status": "replied" if result else "unconfirmed; check X before retrying", "url": result}

@mcp.tool()
async def create_post(text: str, confirm: bool = False) -> dict:
    """Publish a post. Returns its URL (reverse with delete_post). Only on explicit request."""
    gate(confirm)
    if not text.strip():
        raise ValueError("empty post")
    result = await _write("https://x.com/compose/post", '[data-testid="tweetButton"]', text, None)
    return {"status": "posted" if result else "unconfirmed; check X before retrying", "url": result}

async def _handle(page):
    """Logged-in @handle, cached."""
    global _me
    if _me:
        return _me
    await open_x(page, "https://x.com/home")
    btn = page.locator('[data-testid="SideNav_AccountSwitcher_Button"]')
    if await btn.count():
        m = re.search(r"@(\w+)", await btn.inner_text())
        if m:
            _me = m.group(1)
            return _me
    raise RuntimeError("couldn't read handle; is the browser logged in?")

@mcp.tool()
async def whoami() -> str:
    """The @handle of the account signed in to the local browser profile."""
    async with tab_ctx() as page:
        return await _handle(page)

# One probe -> the TARGET post's state. The target is the article that links to its own status id:
# on a reply permalink the FIRST article is the parent, so we must match by id or we delete the wrong
# post. "gone" = already deleted / 404 (success); "deletable" = your post, menu available; "wait" = retry.
DELSTATE_JS = """(sid) => {
  const mine = [...document.querySelectorAll('article')]
     .find(a => a.querySelector(`a[href*="/status/${sid}"]`));
  if (mine && mine.querySelector('[data-testid="caret"]')) return 'deletable';
  if (/doesn.?t exist|was deleted|isn.?t available|not available|page doesn.?t exist|Hmm/i
       .test(document.body.innerText || '')) return 'gone';
  return 'wait';
}"""

def _sid(url):
    m = re.search(r"/status/(\d+)", url)
    if not m:
        raise ValueError(f"not a post URL: {url}")
    return m.group(1)

async def _delstate(page, url, sid):
    await open_x(page, url)
    return await page.evaluate(DELSTATE_JS, sid)

@mcp.tool()
async def delete_post(url: str, confirm: bool = False) -> str:
    """Permanently delete one of YOUR OWN posts OR replies/comments by URL. Only on explicit request.
    Idempotent: a post that's already gone returns "already deleted" rather than erroring."""
    if not confirm:
        raise ValueError("Set confirm=true only when the user explicitly asked to delete this post.")
    sid = _sid(url)
    async def do():
        async with tab_ctx() as page:
            # Land on a clean page first: navigating straight from a just-deleted 404 leaves X's
            # SPA unable to render the next article. Cooldown absorbs the post-write-burst soft-block.
            with contextlib.suppress(Exception):
                await page.goto("https://x.com/home")
                await page.wait_for_timeout(1200)
            state = "wait"
            for attempt in range(6):  # ~6+12+18+24+30 = 90s, beats X's ~1 min soft-block
                state = await _delstate(page, url, sid)
                if state in ("deletable", "gone"):
                    break
                await page.wait_for_timeout(6000 * (attempt + 1))
                with contextlib.suppress(Exception):
                    await page.reload()
                    await page.wait_for_timeout(3000)
            if state == "gone":
                return "already deleted"
            if state != "deletable":
                raise RuntimeError("post never rendered (X soft-block or not your post); try again later")
            # Target the article for THIS id (not article.first, which on a reply is the parent).
            art = page.locator(f'article:has(a[href*="/status/{sid}"])').first
            await _click(art.locator('[data-testid="caret"]').first)
            item = page.get_by_role("menuitem", name="Delete")
            await item.wait_for(state="visible", timeout=8000)
            await _click(item)
            confirm_btn = page.locator('[data-testid="confirmationSheetConfirm"]')
            await confirm_btn.wait_for(state="visible", timeout=8000)
            await _click(confirm_btn)
            await page.wait_for_timeout(1800)
            # Verify it's actually gone, so "deleted" never lies if the confirm click was swallowed.
            if await _delstate(page, url, sid) == "deletable":
                raise RuntimeError("delete confirmed but post still present")
            return "deleted"
    return await _retry1(do)

@mcp.tool()
async def close_browser() -> str:
    """Close the Twitter browser window (needed before running x_login.py, which shares the profile)."""
    global _ctx, _page
    async with lock:
        if _ctx is not None:
            with contextlib.suppress(Exception):
                await _ctx.close()
            _ctx = _page = None
    return "closed"

# ---------- no-login read + drafting queue ----------
@mcp.tool()
def get_tweet(url: str) -> dict:
    """One tweet by URL, no login (oEmbed): text and author only."""
    parsed = urllib.parse.urlsplit(url)
    if parsed.scheme != "https" or parsed.hostname not in ("x.com", "twitter.com", "www.x.com", "www.twitter.com") or parsed.username or parsed.password or parsed.port not in (None, 443):
        raise ValueError("Only HTTPS X/Twitter URLs are allowed")
    u = "https://publish.x.com/oembed?omit_script=1&url=" + urllib.parse.quote(url, safe="")
    with urllib.request.urlopen(u, timeout=20) as r:
        return json.load(r)

def _load():
    return json.loads(QUEUE.read_text()) if QUEUE.exists() else []

@mcp.tool()
def queue_action(kind: str, text: str = "", target_url: str = "") -> dict:
    """Save a draft (post|reply|like) for later review; nothing is sent."""
    if kind not in ("post", "reply", "like"):
        raise ValueError("kind must be post, reply or like")
    q = _load()
    item = {"id": len(q) + 1, "kind": kind, "text": text, "target_url": target_url,
            "queued": datetime.datetime.now().isoformat(timespec="seconds"), "status": "pending"}
    QUEUE.write_text(json.dumps(q + [item], indent=1))
    return item

@mcp.tool()
def list_queue() -> list[dict]:
    """Show saved drafts."""
    return _load()

if __name__ == "__main__":
    mcp.run()

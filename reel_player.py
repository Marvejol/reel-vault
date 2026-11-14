#!/usr/bin/env python3
"""reel_player.py: plays the embedded reels of a page one after another, with no clicks.

    pip install playwright            (once; if Chrome isn't installed: playwright install chromium)
    python reel_player.py             (your Reel Vault)
    python reel_player.py URL --loop

Opens a separate Chrome window with its own profile (Chrome 136+ refuses remote control of
your normal profile). In that window: ⏯ in Chrome's media control / media keys pauses,
⏭ ⏮ skip. Close the window or press Ctrl+C to quit.

Per reel the player runs a small state machine:
  LOAD    find the reel's iframe (by position on the page), scroll it into view, wait for its video
  START   play it from code; if that doesn't take, click it like a user would; if it still
          doesn't play (cookie or login prompt in a fresh profile), ask you to click it once
  WATCH   until: the video fires 'ended' (sticky flag: Instagram resets the video right after),
          ⏭ / ⏮ from the media control, the frame is reloaded/removed, or it stalls
          (playing but time frozen for STALL_S seconds). A pause you make just waits.
"""
import argparse, asyncio, time
from pathlib import Path
from playwright.async_api import async_playwright, Error as PWError

SITE = "https://marvejol.github.io/reel-vault/"
FALLBACK = "https://raw.githack.com/Marvejol/reel-vault/main/index.html"   # used while GitHub Pages is off
PROFILE = Path.home() / ".reel-player-profile"
LOAD_S, START_S, ASK_S, STALL_S = 15, 6, 120, 15

# ---- code that runs inside a reel's frame -------------------------------------------------
HAS_VIDEO = "() => !!document.querySelector('video')"

ARM = """([title]) => {
  // Sticky flags + ⏮ ⏭ in Chrome's media control, installed from inside this frame.
  const v = document.querySelector('video'); if (!v) return false;
  window.__rp = { ended: false, cmd: null, played: false };
  if (!v.__rpHooked) {
    v.__rpHooked = true;
    v.addEventListener('ended', () => { if (window.__rp) window.__rp.ended = true; });
    v.addEventListener('playing', () => { if (window.__rp) window.__rp.played = true; });
  }
  if ('mediaSession' in navigator) {
    navigator.mediaSession.metadata = new MediaMetadata({ title, artist: 'reel_player' });
    navigator.mediaSession.setActionHandler('nexttrack', () => { window.__rp.cmd = 'next'; });
    navigator.mediaSession.setActionHandler('previoustrack', () => { window.__rp.cmd = 'prev'; });
  }
  return true;
}"""

PLAY = """async () => {
  const v = document.querySelector('video'); if (!v) return 'novideo';
  v.currentTime = 0; v.muted = false;
  try { await v.play(); return 'ok'; } catch (e) { return 'blocked:' + e.name; }
}"""

STATE = """() => {
  const v = document.querySelector('video'), r = window.__rp || {};
  return { has: !!v, playing: !!v && !v.paused && !v.ended, paused: !!v && v.paused,
           t: v ? v.currentTime : 0, ended: !!r.ended, played: !!r.played, cmd: r.cmd || null };
}"""

PAUSE = "() => document.querySelectorAll('video,audio').forEach(m => { try { m.pause(); } catch (e) {} })"

# ---- code that runs in your site's page ----------------------------------------------------
QUIET_SITE = """() => {
  // Reel Vault's own silent track, ⏮ ⏭ and frame reloading would fight the player: switch them off.
  try { if (typeof keeper !== 'undefined' && keeper) keeper.pause(); } catch (e) {}
  try { if (typeof timer !== 'undefined') clearTimeout(timer); } catch (e) {}
  const so = document.querySelector('#stopOthers'); if (so) so.checked = false;
  if ('mediaSession' in navigator) for (const a of ['nexttrack', 'previoustrack'])
    try { navigator.mediaSession.setActionHandler(a, null); } catch (e) {}
}"""


class Closed(Exception):
    pass


def log(msg):
    print(time.strftime("%H:%M:%S"), msg, flush=True)


class Player:
    def __init__(self, page, loop):
        self.page, self.loop = page, loop

    def alive(self):
        if self.page.is_closed():
            raise Closed()

    async def sleep(self, s):
        self.alive()
        await asyncio.sleep(s)
        self.alive()

    async def safe(self, frame, js, arg=None, default=None):
        """Evaluate in a frame; a frame that is reloading or gone just gives `default`."""
        self.alive()
        if frame is None:
            return default
        try:
            return await (frame.evaluate(js, arg) if arg is not None else frame.evaluate(js))
        except PWError:
            self.alive()
            return default

    async def iframes(self):
        self.alive()
        try:
            return await self.page.query_selector_all("iframe")
        except PWError:
            self.alive()
            return []

    async def frame_of(self, idx):
        els = await self.iframes()
        if idx >= len(els):
            return None, None
        try:
            return els[idx], await els[idx].content_frame()
        except PWError:
            self.alive()
            return None, None

    # -- page setup --
    async def open(self, url):
        try:
            resp = await self.page.goto(url, wait_until="domcontentloaded")
        except PWError as e:
            self.alive()
            raise SystemExit(f"Can't open {url}: {str(e).splitlines()[0]} (offline, or wrong address?)")
        if url == SITE and resp is not None and resp.status == 404:
            log("GitHub Pages isn't on for the repo yet (404): using the raw.githack copy")
            await self.page.goto(FALLBACK, wait_until="domcontentloaded")
        # raw.githack shows a notice page on first visit: continue through it.
        link = self.page.get_by_text("Open the page", exact=False)
        if await link.count():
            log("Skipping raw.githack's notice page")
            await link.first.click()
            await self.page.wait_for_load_state("domcontentloaded")
        if await self.page.query_selector("#start"):
            await self.page.click("#start")
        deadline = time.time() + 30
        while not await self.iframes():
            if time.time() > deadline:
                log("No embedded reels on this page yet. Waiting (press Start on the page if it has one)…")
                deadline = time.time() + 30
            await self.sleep(0.5)
        await self.safe(self.page.main_frame, QUIET_SITE)

    # -- one reel: LOAD -> START -> WATCH; returns +1 / -1 --
    async def play_reel(self, idx, total):
        title = f"Reel {idx + 1} / {total}"
        el, fr = await self.frame_of(idx)
        if fr is None:
            log(f"{title}: gone from the page, skipping")
            return +1
        try:
            await el.scroll_into_view_if_needed(timeout=3000)
        except PWError:
            self.alive()

        for k, other in enumerate(await self.iframes()):          # only one reel at a time
            if k != idx:
                try:
                    await self.safe(await other.content_frame(), PAUSE)
                except PWError:
                    self.alive()

        # LOAD: wait for the reel's page to load, then briefly for its video element
        try:
            await fr.wait_for_load_state("load", timeout=LOAD_S * 1000)
        except PWError:
            self.alive()
        t_end = time.time() + 3
        while not await self.safe(fr, HAS_VIDEO, default=False) and time.time() < t_end:
            await self.sleep(0.3)
            el, fr = await self.frame_of(idx)
            if fr is None:
                return +1

        # START: from code, then a real click, then ask you
        started = False
        if await self.safe(fr, HAS_VIDEO, default=False):
            await self.safe(fr, ARM, [title])
            r = await self.safe(fr, PLAY, default="error")
            started = await self.wait_playing(fr, 2)
            if not started:
                log(f"{title}: play from code didn't take ({r}), clicking it")
        if not started:
            await self.click(el)
            started = await self.wait_playing(fr, START_S, rearm=title)
        if not started:
            log(f"{title}: won't start by itself (cookie/login prompt?). Click it once in the window…")
            started = await self.wait_playing(fr, ASK_S, rearm=title)
        if not started:
            log(f"{title}: skipped (didn't start)")
            return +1

        log(f"▶ {title}")
        # WATCH
        last_t, last_move = -1.0, time.time()
        while True:
            await self.sleep(0.3)
            s = await self.safe(fr, STATE)
            if s is None:                                          # frame reloaded or removed
                log(f"{title}: frame reloaded, moving on")
                return +1
            if s["cmd"] == "next":
                log("⏭"); return +1
            if s["cmd"] == "prev":
                log("⏮"); return -1
            if s["ended"]:
                return +1
            if not s["has"] and s["played"]:                       # player swapped for an end screen
                return +1
            if s["playing"]:
                if s["t"] != last_t:
                    last_t, last_move = s["t"], time.time()
                elif time.time() - last_move > STALL_S:
                    log(f"{title}: stalled for {STALL_S}s, skipping")
                    return +1
            else:
                last_move = time.time()                            # your pause: just wait

    async def wait_playing(self, fr, seconds, rearm=None):
        t_end = time.time() + seconds
        while time.time() < t_end:
            s = await self.safe(fr, STATE)
            if s and s["playing"]:
                if rearm:
                    await self.safe(fr, ARM, [rearm])              # video may be new after a click
                return True
            await self.sleep(0.3)
        return False

    async def click(self, el):
        """A real mouse click in the middle of the reel, like a user's."""
        try:
            box = await el.bounding_box()
            if box:
                await self.page.mouse.click(box["x"] + box["width"] / 2, box["y"] + box["height"] / 2)
        except PWError:
            self.alive()

    async def run(self):
        i = 0
        while True:
            total = len(await self.iframes())
            if total == 0:
                log("No reels on the page.")
                return
            if i >= total:
                if not self.loop:
                    log("Playlist finished.")
                    return
                i = 0
            i = max(0, i + await self.play_reel(i, total))


async def app_page(ctx):
    """The --app window's page; close the blank window Playwright opens next to it."""
    for _ in range(30):
        app = [pg for pg in ctx.pages if pg.url.startswith("http")]
        if app:
            break
        await asyncio.sleep(0.2)
    page = app[0] if app else (ctx.pages[0] if ctx.pages else await ctx.new_page())
    for pg in ctx.pages:
        if pg is not page:
            try: await pg.close()
            except PWError: pass
    return page


async def main(url, loop, keep_open, headless):
    async with async_playwright() as p:
        opts = dict(headless=headless, no_viewport=True,
                    # --app: a bare app window (no tabs, no address bar), like an installed web app
                    args=["--autoplay-policy=no-user-gesture-required", f"--app={url}",
                          "--window-size=1280,900", "--no-default-browser-check"],
                    # hides the "Chrome is being controlled by automated test software" bar
                    ignore_default_args=["--enable-automation"])
        ctx = None
        for extra in ({"channel": "chrome", "chromium_sandbox": True},   # your Chrome, sandbox on
                      {"chromium_sandbox": True},                        # no Chrome: Playwright's Chromium
                      {"channel": "chrome"}, {}):                        # last resort: sandbox can't start here
            try:
                ctx = await p.chromium.launch_persistent_context(str(PROFILE), **opts, **extra)
                break
            except PWError:
                continue
        if ctx is None:
            raise SystemExit("Couldn't start Chrome or Chromium (try: playwright install chromium)")
        page = await app_page(ctx)
        try:
            player = Player(page, loop)
            await player.open(url)
            await player.run()
            if keep_open:
                log("Done. The window stays open; close it or press Ctrl+C to quit.")
                while not page.is_closed():
                    await asyncio.sleep(0.5)
        except Closed:
            log("Window closed, bye.")
        except PWError:
            if not page.is_closed():
                raise
            log("Window closed, bye.")
        finally:
            try:
                await ctx.close()
            except PWError:
                pass


if __name__ == "__main__":
    ap = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("url", nargs="?", default=SITE)
    ap.add_argument("--loop", action="store_true", help="start again after the last reel")
    ap.add_argument("--close", action="store_true", help="close the window when the playlist ends")
    ap.add_argument("--headless", action="store_true", help=argparse.SUPPRESS)
    a = ap.parse_args()
    try:
        asyncio.run(main(a.url, a.loop, not a.close, a.headless))
    except KeyboardInterrupt:
        print("\nbye")

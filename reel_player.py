#!/usr/bin/env python3
"""reel_player.py: plays every embedded video on a page one after another, inside iframes too.

    pip install playwright          (once)
    python reel_player.py           (opens your Reel Vault)
    python reel_player.py URL --loop

It opens a separate Chrome window with its own profile (Chrome 136+ refuses remote control
of your normal profile) and autoplay allowed, so no clicks are needed. Chrome's media
control / media keys: ⏯ pauses, ⏭ ⏮ skip. Ctrl+C to quit.
"""
import argparse, asyncio, sys
from pathlib import Path
from playwright.async_api import async_playwright, Error as PWError

SITE = "https://marvejol.github.io/reel-vault/"
PROFILE = Path.home() / ".reel-player-profile"

# Runs inside a frame: find its main video/audio.
FIND = """() => { const m = [...document.querySelectorAll('video,audio')];
  return m.length ? m.length : 0; }"""

PLAY = """async ([title]) => {
  const v = document.querySelector('video') || document.querySelector('audio');
  if (!v) return 'none';
  window.__rp_cmd = null;
  v.currentTime = 0; v.muted = false;
  try { await v.play(); } catch (e) { v.muted = true; try { await v.play(); } catch (e2) { return 'blocked:' + e2.name; } }
  if ('mediaSession' in navigator) {               // ⏮ ⏭ in Chrome's media control, from inside this frame
    navigator.mediaSession.metadata = new MediaMetadata({ title, artist: 'reel_player' });
    navigator.mediaSession.setActionHandler('nexttrack', () => { window.__rp_cmd = 'next'; });
    navigator.mediaSession.setActionHandler('previoustrack', () => { window.__rp_cmd = 'prev'; });
  }
  return v.muted ? 'muted' : 'ok';
}"""

STATE = """() => { const v = document.querySelector('video') || document.querySelector('audio');
  return v ? { ended: v.ended, t: v.currentTime, d: v.duration || 0, cmd: window.__rp_cmd || null } : null; }"""

PAUSE = "() => document.querySelectorAll('video,audio').forEach(m => m.pause())"

# Reel Vault has its own silent track + ⏮ ⏭ handlers and reloads frames on "next".
# The app takes over: silence those so Chrome's media control goes to the playing reel.
QUIET_SITE = """() => {
  try { if (typeof keeper !== 'undefined' && keeper) keeper.pause(); } catch (e) {}
  try { if (typeof timer !== 'undefined') clearTimeout(timer); } catch (e) {}
  const so = document.querySelector('#stopOthers'); if (so) so.checked = false;
  if ('mediaSession' in navigator) for (const a of ['nexttrack', 'previoustrack'])
    try { navigator.mediaSession.setActionHandler(a, null); } catch (e) {}
  window.__reelPlayer = true;
}"""


async def media_frames(page):
    """Frames (in page order) that contain a video/audio, top page first, then each iframe."""
    out = []
    for fr in [page.main_frame] + [await h.content_frame() for h in await page.query_selector_all("iframe")]:
        if fr is None: continue
        try:
            if await fr.evaluate(FIND): out.append(fr)
        except PWError:
            pass
    return out


async def run(url, loop, keep_open, headless=False):
    async with async_playwright() as p:
        opts = dict(headless=headless, args=["--autoplay-policy=no-user-gesture-required"], no_viewport=True)
        try:
            ctx = await p.chromium.launch_persistent_context(str(PROFILE), channel="chrome", **opts)
        except PWError:                                     # Chrome not installed: use Playwright's Chromium
            ctx = await p.chromium.launch_persistent_context(str(PROFILE), **opts)
        page = ctx.pages[0] if ctx.pages else await ctx.new_page()
        await page.goto(url)
        if await page.query_selector("#start"):              # Reel Vault: press its Start button
            await page.click("#start")
        await page.wait_for_timeout(4000)                    # let the embeds load
        await page.evaluate(QUIET_SITE)

        i = 0
        while True:
            frames = await media_frames(page)
            if not frames:
                print("No videos found yet, retrying…"); await page.wait_for_timeout(3000); continue
            if i >= len(frames):
                if not loop: break
                i = 0
            for k, fr in enumerate(frames):                  # only one plays at a time
                if k != i:
                    try: await fr.evaluate(PAUSE)
                    except PWError: pass
            fr = frames[i]
            title = f"Reel {i + 1} / {len(frames)}"
            try:
                if fr != page.main_frame:
                    await (await fr.frame_element()).scroll_into_view_if_needed()
            except PWError:
                pass
            r = await fr.evaluate(PLAY, [title])
            print(f"▶ {title}  ({r})  {fr.url[:70]}")
            step = 1
            while True:                                       # wait for end or ⏭ / ⏮
                await page.wait_for_timeout(400)
                try:
                    s = await fr.evaluate(STATE)
                except PWError:                               # frame reloaded or removed: move on
                    break
                if s is None or s["ended"]: break
                if s["cmd"] == "next": break
                if s["cmd"] == "prev": step = -1; break
            i = max(0, i + step)
        print("Playlist finished.")
        if keep_open:
            print("Window stays open; Ctrl+C to quit.")
            await page.wait_for_event("close", timeout=0)
        await ctx.close()


if __name__ == "__main__":
    ap = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("url", nargs="?", default=SITE)
    ap.add_argument("--loop", action="store_true", help="start again after the last one")
    ap.add_argument("--close", action="store_true", help="close the window when done")
    ap.add_argument("--headless", action="store_true", help=argparse.SUPPRESS)
    a = ap.parse_args()
    try:
        asyncio.run(run(a.url, a.loop, not a.close, a.headless))
    except KeyboardInterrupt:
        sys.exit(0)

"""Faithful DOM-snapshot renderer — a dedicated, isolated headless Chromium
living inside THIS container (own Playwright install, own Chromium binary —
see Dockerfile), used only to render a DOM snapshot captured from a live
project tab.

Deliberately NOT the shared aw-sandbox aw-browser/Playwright-MCP session —
that one is a general-purpose shared instance used for lots of other things
across the workspace; this is a small, single-purpose renderer scoped to
this app, matching the same "isolated, dedicated, lazy-launched, idle-
reaped" shape as BackendSupervisor's per-project mock backends.

Why this beats html2canvas (main.py's get_dom(as_image=true) canvas path):
a real Chromium engine paints the page, so CSS features html2canvas only
approximates (object-fit, etc.) render correctly, and a native screenshot
has no canvas-taint/CORS restriction at all (that restriction only applies
to *reading pixels back out* via canvas.toDataURL(), not to displaying an
image or screenshotting the page natively).

The captured HTML is a frozen snapshot, not a live page — <script> tags are
stripped before rendering so nothing re-executes (no re-fetching /api/*,
no resetting whatever JS-driven state was captured in the outerHTML, e.g.
an open lightbox). Only markup + CSS get rendered, same philosophy as
rrweb-style session replay: replay the recorded DOM, don't re-run the app.
"""

from __future__ import annotations

import asyncio
import re
import time

from playwright.async_api import async_playwright

IDLE_TIMEOUT_SECONDS = 15 * 60
_SCRIPT_RE = re.compile(r"<script\b[^>]*>.*?</script>", re.IGNORECASE | re.DOTALL)


def strip_scripts(html: str) -> str:
    return _SCRIPT_RE.sub("", html)


def inject_base(html: str, base_url: str) -> str:
    """Insert <base href> right after <head> so the captured page's relative
    stylesheet/asset links resolve against the live project origin instead
    of wherever this snapshot happens to be rendered from."""
    base_tag = f'<base href="{base_url}">'
    if "<head>" in html:
        return html.replace("<head>", f"<head>{base_tag}", 1)
    if "<head" in html:
        idx = html.index("<head")
        end = html.index(">", idx) + 1
        return html[:end] + base_tag + html[end:]
    return f"<head>{base_tag}</head>{html}"


class SnapshotRenderer:
    """Lazy-launched, persistent, idle-reaped headless Chromium — launching
    a fresh browser process per screenshot would make every capture pay a
    ~1-2s cold-start cost; keeping one warm amortizes that across calls."""

    def __init__(self) -> None:
        self._playwright = None
        self._browser = None
        self._last_used = 0.0
        self._lock = asyncio.Lock()

    async def _ensure_browser(self):
        if self._browser is not None:
            return self._browser
        self._playwright = await async_playwright().start()
        self._browser = await self._playwright.chromium.launch(headless=True)
        return self._browser

    async def render(
        self, html: str, width: int, height: int, full_page: bool,
        scroll_to: tuple[int, int] | None = None,
    ) -> bytes:
        async with self._lock:  # one page at a time — simplicity over throughput for a dev tool
            browser = await self._ensure_browser()
            self._last_used = time.time()
            page = await browser.new_page(viewport={"width": width, "height": height})
            try:
                await page.set_content(html, wait_until="networkidle")
                if scroll_to is not None:
                    # Reproduces the live tab's own scroll position — set_content
                    # always starts a fresh page scrolled to (0, 0), so without
                    # this a viewport-only capture would show the top of the
                    # page instead of whatever the user actually had in view.
                    await page.evaluate(f"window.scrollTo({scroll_to[0]}, {scroll_to[1]})")
                return await page.screenshot(full_page=full_page)
            finally:
                await page.close()

    async def reap_if_idle(self) -> None:
        if self._browser is not None and time.time() - self._last_used > IDLE_TIMEOUT_SECONDS:
            await self._browser.close()
            await self._playwright.stop()
            self._browser = None
            self._playwright = None


renderer = SnapshotRenderer()

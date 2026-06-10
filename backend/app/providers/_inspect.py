"""Quick health check of Grok page state."""
import asyncio
import httpx
import re
import sys
from playwright.async_api import async_playwright


async def main(container: str):
    async with httpx.AsyncClient(timeout=10) as c:
        info = (await c.get(f"http://{container}:9223/json/version")).json()
    ws_url = re.sub(r"ws://[^/]+", f"ws://{container}:9223", info["webSocketDebuggerUrl"])
    async with async_playwright() as p:
        browser = await p.chromium.connect_over_cdp(ws_url, timeout=10000)
        ctx = browser.contexts[0]
        page = ctx.pages[0] if ctx.pages else await ctx.new_page()
        await page.goto("https://grok.com/imagine", wait_until="networkidle", timeout=30000)
        await asyncio.sleep(8)  # full SPA render
        print("URL:", page.url, "Title:", await page.title())

        body = await page.evaluate("() => document.body.innerText.slice(0, 600).replace(/\\n/g, ' | ')")
        print("Body:", body[:500])
        print()

        all_visible_btns = await page.evaluate(
            """() => Array.from(document.querySelectorAll('button')).filter(b => b.offsetParent !== null).map(b => (b.innerText||'').trim()).filter(t => t).slice(0, 30)"""
        )
        print("Visible buttons:", all_visible_btns)

        tiptap = await page.query_selector(".tiptap, .ProseMirror, [contenteditable='true']")
        print("Tiptap exists:", tiptap is not None)

        signin_visible = await page.evaluate(
            """() => {
                const btns = Array.from(document.querySelectorAll('a, button')).filter(b => b.offsetParent !== null);
                return btns.some(b => /^sign\\s?in$/i.test((b.innerText||'').trim()));
            }"""
        )
        print("'Sign in' visible:", signin_visible)

        await browser.disconnect()


if __name__ == "__main__":
    asyncio.run(main(sys.argv[1] if len(sys.argv) > 1 else "grokflow-vnc-db42386164c0"))

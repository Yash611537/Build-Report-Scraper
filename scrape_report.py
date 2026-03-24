#!/usr/bin/env python3
"""
scrape_report.py
----------------
Captures the full partners.build.ai factory report (all sections + videos)
into a self-contained offline HTML file.

Key techniques:
  • Network request interception  → catches every video/blob URL the JS loads
  • Explicit nav-link clicking     → captures Overview, Worker Analysis, Improvement Plan
  • Asset download + inlining      → CSS/JS/images base64-inlined; videos saved to disk

Requirements:
    pip install playwright beautifulsoup4 requests
    playwright install chromium

Usage:
    python scrape_report.py
    # First run opens a browser for Google login; subsequent runs reuse saved auth.
"""

import asyncio
import base64
import hashlib
import mimetypes
import os
import re
import urllib.parse
from pathlib import Path

import requests
from bs4 import BeautifulSoup
from playwright.async_api import async_playwright, Request, Response

# ── Config ────────────────────────────────────────────────────────────────────

REPORT_URL = (
    "https://partners.build.ai/factory-report/"
    "ddf7d5a5-77f5-514a-affc-b33ed27e44f6?recording_date=2026-03-09"
)

OUTPUT_DIR  = Path("report_output")
ASSETS_DIR  = OUTPUT_DIR / "assets"
VIDEOS_DIR  = OUTPUT_DIR / "videos"
OUTPUT_HTML = OUTPUT_DIR / "report.html"

AUTH_STATE       = Path("auth_state.json")
INLINE_THRESHOLD = 512 * 1024   # base64-inline assets < 512 KB

# ── Helpers ───────────────────────────────────────────────────────────────────

def _slug(url: str) -> str:
    digest = hashlib.md5(url.encode()).hexdigest()[:8]
    name   = Path(urllib.parse.urlparse(url).path).name or "asset"
    stem, ext = os.path.splitext(name)
    stem = re.sub(r"[^\w\-]", "_", stem)[:40]
    return f"{stem}_{digest}{ext}" if ext else f"{stem}_{digest}"

def _mime_to_ext(mime: str) -> str:
    return mimetypes.guess_extension(mime.split(";")[0].strip()) or ""

def _is_video_url(url: str, ct: str = "") -> bool:
    ext = Path(urllib.parse.urlparse(url).path).suffix.lower()
    return ext in {".mp4", ".webm", ".ogg", ".mov", ".m4v", ".mkv", ".ts"} \
           or ct.startswith("video/") \
           or "video" in url.lower()

def _data_uri(data: bytes, mime: str) -> str:
    return f"data:{mime};base64,{base64.b64encode(data).decode()}"

# ── Step 1 – Authenticate ─────────────────────────────────────────────────────

async def authenticate() -> None:
    print("\n🔐  Opening browser for Google login …")
    async with async_playwright() as p:
        browser = await p.chromium.launch(headless=False)
        ctx     = await browser.new_context()
        page    = await ctx.new_page()
        await page.goto(REPORT_URL)
        print("   → Sign in with Google.")
        print("   → Wait until the report is FULLY loaded (videos visible),")
        print("     then press ENTER here.")
        input("   [ENTER when done] ")
        await ctx.storage_state(path=str(AUTH_STATE))
        print(f"   ✓ Auth saved → {AUTH_STATE}")
        await browser.close()

# ── Step 2 – Slow scroll helper ───────────────────────────────────────────────

async def _scroll_page(page) -> None:
    await page.evaluate("""
        () => new Promise(resolve => {
            let y = 0;
            const id = setInterval(() => {
                window.scrollTo(0, y);
                y += 200;
                if (y > document.body.scrollHeight) {
                    clearInterval(id);
                    window.scrollTo(0, 0);
                    setTimeout(resolve, 600);
                }
            }, 50);
        })
    """)

# ── Step 3 – Fetch all sections with network interception ─────────────────────

async def fetch_all_sections() -> tuple[list[tuple[str,str]], str, str, dict[str,str]]:
    """
    Returns:
        sections       – [(label, inner_html), …]
        base_url       – final URL
        shell_html     – full page HTML (for <head>)
        video_url_map  – {original_src: local_relative_path}
    """

    # All network-seen video URLs collected here
    intercepted_videos: dict[str, bytes] = {}   # url -> raw bytes (if small enough to buffer)
    intercepted_video_urls: list[str] = []       # ordered list of video URLs seen

    print("\n📥  Loading report (network interception enabled) …")

    async with async_playwright() as p:
        browser = await p.chromium.launch(headless=False)
        ctx     = await browser.new_context(
            storage_state=str(AUTH_STATE),
            # Record all responses so we can grab video bytes directly
        )
        page = await ctx.new_page()
        page.set_default_timeout(90_000)

        # ── Intercept responses to catch video URLs ──
        async def on_response(response: Response):
            url = response.url
            ct  = response.headers.get("content-type", "")
            if _is_video_url(url, ct):
                if url not in intercepted_video_urls:
                    intercepted_video_urls.append(url)
                    print(f"   🎬  Intercepted video: {url[:100]}")
                    try:
                        body = await response.body()
                        intercepted_videos[url] = body
                    except Exception:
                        pass   # large streaming videos won't buffer — we'll download later

        page.on("response", on_response)

        # ── Also watch for video src set via JS ──
        await page.add_init_script("""
            () => {
                const orig = Object.getOwnPropertyDescriptor(HTMLMediaElement.prototype, 'src');
                if (!orig) return;
                Object.defineProperty(HTMLMediaElement.prototype, 'src', {
                    set(val) {
                        if (val && !val.startsWith('data:')) {
                            window.__capturedVideoSrcs = window.__capturedVideoSrcs || [];
                            if (!window.__capturedVideoSrcs.includes(val))
                                window.__capturedVideoSrcs.push(val);
                        }
                        orig.set.call(this, val);
                    },
                    get: orig.get,
                    configurable: true,
                });
            }
        """)

        # ── Load page ──
        await page.goto(REPORT_URL, wait_until="domcontentloaded", timeout=90_000)

        # Wait for the actual nav links to appear (from screenshot: "Overview", etc.)
        print("   Waiting for nav links …")
        try:
            # The screenshot shows links inside what looks like a left sidebar
            await page.wait_for_selector("text=Overview", timeout=20_000)
            print("   ✓ Nav links visible")
        except Exception:
            print("   ⚠  'Overview' not found — waiting 8 s …")
            await page.wait_for_timeout(8_000)

        await page.wait_for_timeout(2_000)
        base_url = page.url

        # ── Discover the nav section links ──
        # From the screenshot: sidebar has "Overview", "Worker Analysis", "Improvement Plan"
        # Try multiple strategies to find them
        async def find_nav_labels() -> list[str]:
            # Strategy 1: look for links that match known section names
            known = ["Overview", "Worker Analysis", "Improvement Plan"]
            found = []
            for name in known:
                els = await page.query_selector_all(f"text={name}")
                for el in els:
                    tag = await el.evaluate("el => el.tagName.toLowerCase()")
                    if tag in ("a", "button", "li", "span", "div"):
                        found.append(name)
                        break
            if found:
                return found

            # Strategy 2: all sidebar/nav links
            labels = []
            seen   = set()
            for sel in ["nav a", "aside a", "[class*='nav'] a",
                        "[class*='sidebar'] a", "[class*='menu'] button"]:
                els = await page.query_selector_all(sel)
                for el in els:
                    try:
                        lbl = (await el.inner_text()).strip()
                        href = (await el.get_attribute("href") or "").strip()
                        if lbl and lbl not in seen and not href.startswith("http"):
                            seen.add(lbl)
                            labels.append(lbl)
                    except Exception:
                        continue
            return labels

        nav_labels = await find_nav_labels()
        print(f"   ✓ Nav sections: {nav_labels}")

        # ── Capture each section ──
        sections: list[tuple[str, str]] = []
        shell_html = ""

        async def capture_section(label: str) -> str:
            """Click a nav item, wait for content, scroll, return inner HTML."""
            # Find the element with this exact text
            clicked = False
            for sel in [f"text={label}", f"a:has-text('{label}')",
                        f"button:has-text('{label}')", f"li:has-text('{label}')"]:
                try:
                    el = page.locator(sel).first
                    await el.click(timeout=5_000)
                    clicked = True
                    break
                except Exception:
                    continue
            if not clicked:
                print(f"      ⚠  Could not click {label!r}")
                return ""

            await page.wait_for_timeout(2_000)

            # Wait for content to change/load
            for wait_sel in ["h1", "h2", "video", "p", "main", "article"]:
                try:
                    await page.wait_for_selector(wait_sel, timeout=8_000)
                    break
                except Exception:
                    continue

            await _scroll_page(page)
            await page.wait_for_timeout(2_000)

            # Collect any JS-set video srcs
            js_srcs = await page.evaluate("() => window.__capturedVideoSrcs || []")
            for src in js_srcs:
                if src not in intercepted_video_urls:
                    intercepted_video_urls.append(src)
                    print(f"   🎬  JS-set video src: {src[:100]}")

            # Grab content
            for content_sel in ["main", "article", "[class*='content']",
                                 "[class*='report-body']", "#root"]:
                el = await page.query_selector(content_sel)
                if el:
                    inner = await el.inner_html()
                    print(f"      ✓ {label!r} captured via {content_sel}  ({len(inner):,} chars)")
                    return inner

            inner = await page.content()
            print(f"      ⚠  {label!r} fallback to full page  ({len(inner):,} chars)")
            return inner

        if not nav_labels:
            # Single section
            await _scroll_page(page)
            await page.wait_for_timeout(2_000)
            html = await _grab_main(page)
            sections.append(("Report", html))
        else:
            for idx, label in enumerate(nav_labels):
                print(f"\n   [{idx+1}/{len(nav_labels)}] → {label!r}")
                html = await capture_section(label)
                if html:
                    sections.append((label, html))

        shell_html = await page.content()

        # Final JS video src sweep
        js_srcs = await page.evaluate("() => window.__capturedVideoSrcs || []")
        for src in js_srcs:
            if src not in intercepted_video_urls:
                intercepted_video_urls.append(src)

        print(f"\n   ✓ Total video URLs intercepted: {len(intercepted_video_urls)}")
        await browser.close()

    # ── Download videos ──
    VIDEOS_DIR.mkdir(parents=True, exist_ok=True)
    video_url_map: dict[str, str] = {}   # original_url -> relative_path_in_output_dir

    session = requests.Session()
    # Reload cookies from auth state for requests
    import json
    if AUTH_STATE.exists():
        state = json.loads(AUTH_STATE.read_text())
        for ck in state.get("cookies", []):
            session.cookies.set(ck["name"], ck["value"], domain=ck.get("domain",""))

    for url in intercepted_video_urls:
        dest = VIDEOS_DIR / _slug(url)
        if not dest.suffix or dest.suffix == ".":
            dest = dest.with_suffix(".mp4")

        if url in intercepted_videos:
            # We already have the bytes from interception
            dest.write_bytes(intercepted_videos[url])
            size_mb = len(intercepted_videos[url]) / 1_048_576
        else:
            # Download it
            print(f"   ⬇  Downloading {url[:80]} …")
            try:
                r = session.get(url, timeout=120, stream=True)
                r.raise_for_status()
                dest.write_bytes(r.content)
                size_mb = len(r.content) / 1_048_576
            except Exception as e:
                print(f"   ⚠  Failed: {e}")
                continue

        rel = dest.relative_to(OUTPUT_DIR).as_posix()
        video_url_map[url] = rel
        print(f"   ✓ {rel}  ({size_mb:.1f} MB)")

    return sections, base_url, shell_html, video_url_map


async def _grab_main(page) -> str:
    for sel in ["main", "article", "[class*='content']", "#root"]:
        el = await page.query_selector(sel)
        if el:
            return await el.inner_html()
    return await page.content()


# ── Step 4 – Collect cookies ──────────────────────────────────────────────────

async def get_cookies() -> list[dict]:
    async with async_playwright() as p:
        browser = await p.chromium.launch(headless=True)
        ctx     = await browser.new_context(storage_state=str(AUTH_STATE))
        cookies = await ctx.cookies()
        await browser.close()
    return cookies

# ── Step 5 – Build combined HTML ──────────────────────────────────────────────

def build_html(sections: list[tuple[str,str]], shell_html: str) -> str:
    shell_soup = BeautifulSoup(shell_html, "html.parser")
    head = shell_soup.find("head") or BeautifulSoup("<head></head>","html.parser").head

    nav_btns = "\n".join(
        f'<button class="sec-btn{" active" if i==0 else ""}" '
        f'onclick="showSec({i})">{lbl}</button>'
        for i,(lbl,_) in enumerate(sections)
    )
    panels = "\n".join(
        f'<div class="sec-panel" id="sec-{i}" '
        f'style="display:{"block" if i==0 else "none"}">'
        f'<h2 class="sec-heading">{lbl}</h2>{content}</div>'
        for i,(lbl,content) in enumerate(sections)
    )

    return f"""<!DOCTYPE html>
<html lang="en">
{head}
<body style="margin:0;font-family:system-ui,sans-serif;background:#fff">
<div style="display:flex;min-height:100vh">

  <aside style="width:220px;min-width:220px;flex-shrink:0;background:#f9fafb;
                border-right:1px solid #e5e7eb;padding:2rem 1rem;
                position:sticky;top:0;height:100vh;overflow-y:auto;box-sizing:border-box">
    <div style="font-size:.7rem;color:#9ca3af;letter-spacing:.08em;margin-bottom:.4rem">build</div>
    <div style="font-weight:700;font-size:.95rem;margin-bottom:.2rem">DIVINITEE GENESS</div>
    <div style="font-size:.78rem;color:#6b7280;margin-bottom:1.8rem">09 Mar 2026</div>
    <nav style="display:flex;flex-direction:column;gap:2px">
      {nav_btns}
    </nav>
  </aside>

  <main style="flex:1;padding:2.5rem;overflow-x:hidden;min-width:0">
    {panels}
  </main>

</div>

<div style="position:fixed;bottom:0;left:0;right:0;background:#111827;color:#d1d5db;
            font:11px/2 sans-serif;text-align:center;z-index:99999;padding:2px 8px">
  Offline copy — originally from partners.build.ai
</div>

<style>
  .sec-btn {{
    display:block;width:100%;text-align:left;padding:.45rem .75rem;
    border:none;background:none;cursor:pointer;border-radius:6px;
    font-size:.88rem;color:#374151;transition:background .15s;
  }}
  .sec-btn:hover,.sec-btn.active {{ background:#e5e7eb; }}
  .sec-btn.active {{ font-weight:600; }}
  .sec-heading {{
    font-size:1.25rem;margin:0 0 1.25rem;padding-bottom:.6rem;
    border-bottom:1px solid #e5e7eb;color:#111827;
  }}
  video {{ max-width:100%;border-radius:8px;background:#000; }}
</style>
<script>
  function showSec(idx) {{
    document.querySelectorAll('.sec-panel').forEach((el,i) =>
      el.style.display = i===idx ? 'block' : 'none');
    document.querySelectorAll('.sec-btn').forEach((el,i) =>
      el.classList.toggle('active', i===idx));
  }}
</script>
</body></html>"""

# ── Step 6 – Download & rewrite all assets ────────────────────────────────────

def process_assets(
    combined_html: str,
    base_url: str,
    cookies: list[dict],
    video_url_map: dict[str,str],
) -> str:
    OUTPUT_DIR.mkdir(parents=True, exist_ok=True)
    ASSETS_DIR.mkdir(parents=True, exist_ok=True)

    session = requests.Session()
    for ck in cookies:
        session.cookies.set(ck["name"], ck["value"], domain=ck.get("domain",""))

    soup = BeautifulSoup(combined_html, "html.parser")

    def resolve(u: str) -> str:
        return urllib.parse.urljoin(base_url, u)

    def process_url(raw: str) -> str:
        if not raw or raw.startswith("data:") or \
           raw.startswith("#") or raw.startswith("javascript:"):
            return raw

        abs_url = resolve(raw)

        # Already downloaded video?
        if abs_url in video_url_map:
            return video_url_map[abs_url]
        if raw in video_url_map:
            return video_url_map[raw]
        # Check by partial match (handles URL param differences)
        for vid_url, vid_path in video_url_map.items():
            if urllib.parse.urlparse(vid_url).path == urllib.parse.urlparse(abs_url).path:
                return vid_path

        try:
            r  = session.get(abs_url, timeout=60, stream=True)
            r.raise_for_status()
            data = r.content
            ct   = r.headers.get("content-type","application/octet-stream")
        except Exception as e:
            print(f"   ⚠  {abs_url[:80]} → {e}")
            return raw

        if _is_video_url(abs_url, ct):
            dest = VIDEOS_DIR / _slug(abs_url)
            if not dest.suffix:
                dest = dest.with_suffix(_mime_to_ext(ct) or ".mp4")
            dest.write_bytes(data)
            rel = dest.relative_to(OUTPUT_DIR).as_posix()
            print(f"   🎬  {rel}  ({len(data)/1_048_576:.1f} MB)")
            return rel

        mime = ct.split(";")[0].strip() or "application/octet-stream"
        if len(data) < INLINE_THRESHOLD:
            return _data_uri(data, mime)

        dest = ASSETS_DIR / _slug(abs_url)
        if not dest.suffix:
            dest = dest.with_suffix(_mime_to_ext(ct) or ".bin")
        dest.write_bytes(data)
        return dest.relative_to(OUTPUT_DIR).as_posix()

    # Rewrite HTML attributes
    for tag_name, attr in [
        ("link","href"),("script","src"),
        ("img","src"),("img","data-src"),
        ("source","src"),("video","src"),("video","poster"),
    ]:
        for tag in soup.find_all(tag_name, **{attr: True}):
            tag[attr] = process_url(tag[attr])

    # Rewrite CSS url(...)
    for style_tag in soup.find_all("style"):
        if style_tag.string:
            style_tag.string = re.sub(
                r'url\(\s*([^)]+)\s*\)',
                lambda m: f"url({process_url(m.group(1).strip(chr(39)+chr(34)))})",
                style_tag.string,
            )

    # Rewrite srcset
    for img in soup.find_all(srcset=True):
        parts = []
        for part in img["srcset"].split(","):
            tokens = part.strip().split()
            if tokens:
                tokens[0] = process_url(tokens[0])
            parts.append(" ".join(tokens))
        img["srcset"] = ", ".join(parts)

    # Ensure all video tags have controls + correct src from video_url_map
    for video in soup.find_all("video"):
        video["controls"]    = ""
        video["playsinline"] = ""
        # If src is still a remote URL, try to map it
        src = video.get("src","")
        if src and not src.startswith("videos/") and not src.startswith("data:"):
            mapped = process_url(src)
            video["src"] = mapped

        for source in video.find_all("source"):
            src = source.get("src","")
            if src and not src.startswith("videos/") and not src.startswith("data:"):
                source["src"] = process_url(src)

    return str(soup)

# ── Main ──────────────────────────────────────────────────────────────────────

async def main():
    if not AUTH_STATE.exists():
        await authenticate()
    else:
        print(f"ℹ️   Auth state: {AUTH_STATE}  (delete to re-login)\n")

    sections, base_url, shell_html, video_url_map = await fetch_all_sections()

    if not sections:
        print("❌  No content captured. Delete auth_state.json and re-run.")
        return

    print(f"\n   Sections captured: {[s for s,_ in sections]}")
    print(f"   Videos found:      {len(video_url_map)}")

    cookies = await get_cookies()

    print("\n🔧  Building HTML & downloading remaining assets …")
    raw_html   = build_html(sections, shell_html)
    final_html = process_assets(raw_html, base_url, cookies, video_url_map)

    OUTPUT_HTML.write_text(final_html, encoding="utf-8")
    size_mb = OUTPUT_HTML.stat().st_size / 1_048_576
    print(f"\n✅  Done!")
    print(f"   HTML   → {OUTPUT_HTML}  ({size_mb:.1f} MB)")
    print(f"   Videos → {VIDEOS_DIR}/")
    print(f"   Assets → {ASSETS_DIR}/")
    print("\n   Zip report_output/ to share, or open report.html directly in any browser.")

if __name__ == "__main__":
    asyncio.run(main())
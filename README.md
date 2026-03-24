# Report Downloader

This project downloads an authenticated factory report from `partners.build.ai` and creates a shareable offline copy.

The script:
- opens the report in a real browser,
- captures all visible sections (for example: Overview, Worker Analysis, Improvement Plan),
- downloads assets and videos,
- writes a standalone HTML report that can be opened without login.

## What input is required

`scrape_report.py` currently expects:

1. **A report URL** in `REPORT_URL` inside `scrape_report.py`.
2. **Google login access** to that report (interactive browser sign-in on first run).

Optional behavior:
- After first login, auth is stored in `auth_state.json` and reused in future runs.
- Delete `auth_state.json` to force re-login.

## Prerequisites

- Python 3.10+ (recommended)
- pip
- Playwright Chromium browser

Install dependencies:

```bash
pip install playwright beautifulsoup4 requests
playwright install chromium
```

## How to run

From project root:

```bash
python scrape_report.py
```

On first run:
- A browser window opens.
- Sign in with Google.
- Wait until the report is fully visible.
- Press Enter in terminal when prompted.

## What output is produced

The script creates a `report_output/` folder with:

- `report_output/report.html` - main shareable offline report
- `report_output/assets/` - downloaded JS/CSS/images (or inlined data URIs for small files)
- `report_output/videos/` - downloaded video files referenced by the report

Result:
- Open `report_output/report.html` in any browser.
- No login is required for viewers.

## Sharing / hosting

To share with others:
- zip and send `report_output/`, or
- host `report_output/` on static hosting (Netlify, Vercel, GitHub Pages, etc.).

For Netlify/Vercel root URL support, either:
- rename `report.html` to `index.html`, or
- configure a redirect/rewrite from `/` to `/report.html`.

## Notes

- `report_output/` and `auth_state.json` are gitignored by default, so generated files are not committed unless you change `.gitignore`.
- If capture fails, delete `auth_state.json` and run again.

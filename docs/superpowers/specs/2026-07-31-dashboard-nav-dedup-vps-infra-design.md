# Dashboard nav de-duplication + VPS infra stat card — design

## Problem

1. The dashboard's 6 static pages (`index.html`, `generation.html`, `inspector.html`,
   `logs.html`, `search.html`, `cent-capital.html`) each hand-duplicate three nav
   blocks: `.desktop-nav`, `.drawer-links`, `.mobile-bottom-nav` — 18 near-identical
   blocks total. Adding or renaming a page requires editing all 6 files and is
   easy to get wrong (a stale link was already found doing this exploration).
2. `styles.css`, `dashboard.js`, and `docs/*.md` were audited and found NOT
   redundant — no duplicate CSS selectors, no duplicated inline scripts, and the
   docs split cleanly by concern (README = full reference, docs/ = focused
   topic guides). No changes needed there.
3. The VPS (2.24.28.180) runs 4 continuous systemd services relevant to this
   project (`job-app-ashby`, `job-app-greenhouse`, `job-app-lever`,
   `job-app-search-sync`) and had 97 archived application document sets at
   inspection time. None of this is currently visible on the public dashboard,
   even though it's a directly relevant operational fact about the automation.

## Non-goals

- No server-side templating engine, no build step.
- No new SSH-capable route on the public dashboard server (`server.py` is
  intentionally unauthenticated and read-only; this must not change).
- No change to per-page SEO meta tags (title/description/OG/JSON-LD) — those
  legitimately differ per page and are not redundant.

## Design

### 1. Nav de-duplication

- Each HTML file's three nav blocks are replaced with a single placeholder:
  `<div id="site-nav" data-active="index"></div>` (value of `data-active` is
  the page's own slug: `index`, `generation`, `inspector`, `logs`, `search`,
  `cent-capital`).
- `dashboard.js` gains:
  - A single `NAV_PAGES` array of `{slug, href, label, icon}` entries (one
    entry per page, in nav order).
  - A `renderNav()` function that reads `#site-nav`'s `data-active`, builds the
    desktop nav, drawer links, and mobile bottom bar markup from `NAV_PAGES`,
    and injects them, marking the active page's link(s).
  - Called once on `DOMContentLoaded`, before other page-specific init code.
- Existing nav-related CSS classes (`.desktop-nav`, `.drawer-links`,
  `.mobile-bottom-nav`, active-state classes) are unchanged — only where the
  markup comes from changes, not its structure or styling.

### 2. VPS infra stat card

- **VPS side** (`scripts/vps_search_sync.sh`): after the existing search/apply
  stages, write `output/vps_infra_status.json` with:
  ```json
  {
    "generated_at": "<ISO8601 UTC>",
    "active_services": ["job-app-ashby", "job-app-greenhouse", "job-app-lever", "job-app-search-sync"],
    "uptime": "up 15 hours, 42 minutes"
  }
  ```
  `active_services` is populated by checking `systemctl is-active` for the
  four known unit names (not a free-form `systemctl list-units` scrape, to
  keep the output shape stable). Written with the same atomic-write-then-move
  care as the script's other output files.
- **Local side** (`scripts/pull_vps_application_reports.ps1`): add
  `vps_infra_status.json` to the existing `$ReportNames` list it already pscp's
  into `output/vps_reports/`.
- **Server** (`dashboard/server.py`): `build_kpi_metrics()` loads it via the
  existing `load_json_file("vps_infra_status.json", default={})` helper
  (matching every other metrics source) and adds a `vps_infra` key to the
  `/api/metrics` response. No new endpoint.
- **Frontend**: a new small stat card ("Active Engines") on `index.html`,
  rendered by `dashboard.js` from `metrics.vps_infra.active_services` /
  `.uptime`, following the same pattern as the existing KPI cards (render
  nothing / a neutral placeholder if the field is absent, since the file
  won't exist until the next VPS sync after this ships).

## Testing

- `tests/test_dashboard.py`: extend for `build_kpi_metrics()` including
  `vps_infra` when `vps_infra_status.json` is present, and omitting/defaulting
  it cleanly when absent.
- Manual: load each of the 6 pages locally via the dashboard server, confirm
  nav renders identically to today (links, active-state highlighting, mobile
  drawer/bottom bar) with no visual regression, and confirm the new stat card
  renders (with placeholder data) without layout breakage when the field is
  missing.

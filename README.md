# AppLovin Pixel Tracker (BuiltWith)

Weekly tracker of websites using AppLovin's ecommerce pixel, pulled from the
BuiltWith Trends API and rendered as a dashboard (`docs/index.html`).

Everything runs on GitHub Actions — nothing to install locally.

## What runs
| Workflow | When | API credits |
|---|---|---|
| **Backfill history (run once)** | Manually, once | 1 per week of history (~95 from Jan-2025) |
| **Weekly BuiltWith update** | Every Saturday 15:17 UTC + manual button | 6 per run (1 current + this week + 4 rechecks) |

## Files
- `tracker.py` — API pulls, metrics, restatement detection, dashboard build
- `dashboard_template.html` — dashboard layout (data is injected at build time)
- `data/snapshots.csv` — every API pull ever made (raw, never overwritten)
- `data/weekly.csv` — one row per week with WoW %, net adds, 4-wk avg, Top 1M share, churn
- `data/restatements.csv` — weeks BuiltWith revised after the fact
- `data/multiples.csv` — valuation multiples (optional, shown in the Multiples section)
- `docs/index.html` — the dashboard (open in any browser)

## Secret required
`BUILTWITH_API_KEY` — repo **Settings → Secrets and variables → Actions**.

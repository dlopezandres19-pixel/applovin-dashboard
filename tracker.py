"""
AppLovin BuiltWith tracker
==========================

Pulls the BuiltWith Trends API for one technology (default: AppLovin), keeps a
weekly history, recomputes metrics, detects restatements and builds a
self-contained HTML dashboard.

Commands
--------
  python tracker.py backfill [--start 2025-01-04] [--dry-run]
      One-off: fills the weekly history back to --start (1 API credit per week).
      Weeks already stored are skipped, so re-running costs nothing extra.

  python tracker.py weekly [--recheck 4]
      Normal weekly run: 1 "current" pull (the only one that returns Expired)
      + 1 dated pull for this week + re-pulls the previous N weeks to catch
      restatements. Default cost: 6 credits per run.

  python tracker.py build
      Recomputes metrics and writes docs/index.html. No API calls.

The API key is read from the BUILTWITH_API_KEY environment variable
(GitHub secret in Actions, or a local .env file).
"""

from __future__ import annotations

import argparse
import csv
import json
import os
import sys
import time
from datetime import date, datetime, timedelta, timezone
from pathlib import Path

import requests

# ── configuration ────────────────────────────────────────────────────────────
TECH = os.environ.get("BUILTWITH_TECH", "AppLovin")
API_URL = "https://api.builtwith.com/trends/v6/api.json"
ROOT = Path(__file__).resolve().parent
DATA = ROOT / "data"
SNAPSHOTS = DATA / "snapshots.csv"      # every API pull, never overwritten
WEEKLY = DATA / "weekly.csv"            # one row per week, with metrics
RESTATEMENTS = DATA / "restatements.csv"
MULTIPLES = DATA / "multiples.csv"      # optional, filled later from the Excel
DASHBOARD = ROOT / "docs" / "index.html"
TEMPLATE = ROOT / "dashboard_template.html"

ANCHOR_WEEKDAY = 5          # Saturday (BuiltWith chart points fall on Saturdays)
DEFAULT_START = "2025-01-04"
REQUEST_PAUSE_S = 1.0       # be polite with the API

SNAP_FIELDS = ["pulled_at_utc", "mode", "as_of", "ten_k", "hundred_k",
               "top_1m", "live", "live_plus_expired"]
RESTATE_FIELDS = ["detected_at_utc", "as_of", "field", "old_value",
                  "new_value", "diff"]
COUNT_FIELDS = ["live", "top_1m", "hundred_k", "ten_k"]


# ── helpers ──────────────────────────────────────────────────────────────────
def load_env_file() -> None:
    """Minimal .env loader so the script also runs locally."""
    env = ROOT / ".env"
    if not env.exists():
        return
    for line in env.read_text(encoding="utf-8").splitlines():
        line = line.strip()
        if not line or line.startswith("#") or "=" not in line:
            continue
        k, v = line.split("=", 1)
        os.environ.setdefault(k.strip(), v.strip().strip('"').strip("'"))


def api_key() -> str:
    load_env_file()
    key = os.environ.get("BUILTWITH_API_KEY", "").strip()
    if not key:
        sys.exit("ERROR: falta BUILTWITH_API_KEY (secret de GitHub o archivo .env).")
    return key


def now_utc() -> str:
    return datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")


def last_anchor(d: date) -> date:
    """Most recent Saturday on or before d."""
    return d - timedelta(days=(d.weekday() - ANCHOR_WEEKDAY) % 7)


def anchors_between(start: date, end: date) -> list[date]:
    first = start + timedelta(days=(ANCHOR_WEEKDAY - start.weekday()) % 7)
    out, d = [], first
    while d <= end:
        out.append(d)
        d += timedelta(days=7)
    return out


def to_int(v) -> int | None:
    try:
        n = int(float(v))
    except (TypeError, ValueError):
        return None
    return None if n < 0 else n   # API returns -1 when a field is unavailable


def read_csv(path: Path) -> list[dict]:
    if not path.exists():
        return []
    with path.open(newline="", encoding="utf-8") as f:
        return list(csv.DictReader(f))


def append_csv(path: Path, fields: list[str], rows: list[dict]) -> None:
    if not rows:
        return
    path.parent.mkdir(parents=True, exist_ok=True)
    new = not path.exists()
    with path.open("a", newline="", encoding="utf-8") as f:
        w = csv.DictWriter(f, fieldnames=fields)
        if new:
            w.writeheader()
        w.writerows(rows)


def write_csv(path: Path, fields: list[str], rows: list[dict]) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    with path.open("w", newline="", encoding="utf-8") as f:
        w = csv.DictWriter(f, fieldnames=fields)
        w.writeheader()
        w.writerows(rows)


# ── API ──────────────────────────────────────────────────────────────────────
def fetch(key: str, as_of: date | None) -> dict:
    """One API call = 1 credit. as_of=None -> current totals (includes expired)."""
    params = {"KEY": key, "TECH": TECH}
    if as_of:
        params["DATE"] = as_of.isoformat()
    for attempt in range(3):
        try:
            r = requests.get(API_URL, params=params, timeout=60)
            r.raise_for_status()
            payload = r.json()
            break
        except (requests.RequestException, ValueError) as e:
            if attempt == 2:
                raise SystemExit(f"ERROR llamando a BuiltWith ({as_of}): {e}")
            time.sleep(5 * (attempt + 1))
    tech = payload.get("Tech") or payload.get("tech")
    if not tech or "coverage" not in tech:
        msg = json.dumps(payload)[:300]
        raise SystemExit(f"ERROR: respuesta inesperada de BuiltWith: {msg}")
    cov = tech["coverage"]
    return {
        "ten_k": to_int(cov.get("ten_k")),
        "hundred_k": to_int(cov.get("hundred_k")),
        "top_1m": to_int(cov.get("milly")),
        "live": to_int(cov.get("live")),
        # NB: the API's "expired" field is Live + Expired (cumulative ever seen)
        "live_plus_expired": to_int(cov.get("expired")),
    }


def snapshot_row(mode: str, as_of: date, values: dict) -> dict:
    row = {"pulled_at_utc": now_utc(), "mode": mode, "as_of": as_of.isoformat()}
    row.update({k: ("" if values[k] is None else values[k]) for k in values})
    return row


# ── restatement detection ────────────────────────────────────────────────────
def find_restatements(old_snaps: list[dict], new_rows: list[dict]) -> list[dict]:
    """Compare each new dated pull with the latest earlier dated pull for the
    same week. Current-mode pulls are never compared (different query type)."""
    latest: dict[str, dict] = {}
    for s in old_snaps:
        if s["mode"] == "date":
            latest[s["as_of"]] = s
    out = []
    for n in new_rows:
        if n["mode"] != "date" or n["as_of"] not in latest:
            continue
        prev = latest[n["as_of"]]
        for f in COUNT_FIELDS:
            a, b = to_int(prev.get(f)), to_int(n.get(f))
            if a is not None and b is not None and a != b:
                out.append({"detected_at_utc": n["pulled_at_utc"],
                            "as_of": n["as_of"], "field": f,
                            "old_value": a, "new_value": b, "diff": b - a})
    return out


# ── metrics ──────────────────────────────────────────────────────────────────
WEEKLY_FIELDS = [
    "week", "live", "top_1m", "hundred_k", "ten_k", "outside_top_1m",
    "top_1m_share_pct", "net_adds", "wow_pct", "wow_4w_avg_pct",
    "wow_accel_pp", "expired", "live_plus_expired", "gross_adds",
    "churned", "churn_pct", "restated",
]


def pct(a, b):
    return None if a is None or b in (None, 0) else round((a - b) / b * 100, 2)


def build_weekly(snaps: list[dict], restatements: list[dict]) -> list[dict]:
    """One row per anchor week. Counts come from the latest dated pull for that
    week (consistent query type over time); Expired comes from the current pull
    made that week, since dated pulls return -1 for it."""
    dated: dict[str, dict] = {}
    current: dict[str, dict] = {}
    for s in snaps:
        if s["mode"] == "date":
            dated[s["as_of"]] = s                      # later rows win
        else:
            current[s["as_of"]] = s
    restated_weeks = {r["as_of"] for r in restatements}

    weeks = sorted(set(dated) | set(current))
    rows: list[dict] = []
    for w in weeks:
        src = dated.get(w) or current.get(w)
        cur = current.get(w, {})
        r = {"week": w}
        for f in COUNT_FIELDS:
            r[f] = to_int(src.get(f))
        lpe = to_int(cur.get("live_plus_expired"))
        live_cur = to_int(cur.get("live"))
        r["live_plus_expired"] = lpe
        r["expired"] = (lpe - live_cur) if (lpe is not None and live_cur is not None) else None
        r["outside_top_1m"] = (r["live"] - r["top_1m"]) if None not in (r["live"], r["top_1m"]) else None
        r["top_1m_share_pct"] = round(r["top_1m"] / r["live"] * 100, 1) if r["live"] and r["top_1m"] is not None else None
        r["restated"] = "yes" if w in restated_weeks else ""
        rows.append(r)

    for i, r in enumerate(rows):
        p = rows[i - 1] if i else None
        r["net_adds"] = (r["live"] - p["live"]) if p and None not in (r["live"], p["live"]) else None
        r["wow_pct"] = pct(r["live"], p["live"]) if p else None
        last4 = [x["wow_pct"] for x in rows[max(0, i - 3): i + 1] if x["wow_pct"] is not None]
        r["wow_4w_avg_pct"] = round(sum(last4) / len(last4), 2) if len(last4) == 4 else None
        r["wow_accel_pp"] = round(r["wow_pct"] - p["wow_pct"], 2) if p and None not in (r["wow_pct"], p.get("wow_pct")) else None
        # churn: only between two consecutive weeks that both have a current pull
        if p and None not in (r["live_plus_expired"], p["live_plus_expired"], r["expired"], p["expired"]):
            r["gross_adds"] = r["live_plus_expired"] - p["live_plus_expired"]
            r["churned"] = r["expired"] - p["expired"]
            r["churn_pct"] = round(r["churned"] / p["live"] * 100, 2) if p["live"] else None
        else:
            r["gross_adds"] = r["churned"] = r["churn_pct"] = None
    return [{k: ("" if r.get(k) is None else r.get(k)) for k in WEEKLY_FIELDS} for r in rows]


# ── dashboard ────────────────────────────────────────────────────────────────
def build_dashboard() -> None:
    snaps = read_csv(SNAPSHOTS)
    restatements = read_csv(RESTATEMENTS)
    weekly = build_weekly(snaps, restatements)
    write_csv(WEEKLY, WEEKLY_FIELDS, weekly)

    payload = {
        "tech": TECH,
        "generated_at": now_utc(),
        "weekly": weekly,
        "restatements": restatements[-200:],
        "multiples": read_csv(MULTIPLES),
    }
    html = TEMPLATE.read_text(encoding="utf-8").replace(
        "/*__DATA__*/null", json.dumps(payload, ensure_ascii=False).replace("</", "<\\/"))
    DASHBOARD.parent.mkdir(parents=True, exist_ok=True)
    DASHBOARD.write_text(html, encoding="utf-8")
    print(f"OK  {len(weekly)} semanas -> {WEEKLY.relative_to(ROOT)} y {DASHBOARD.relative_to(ROOT)}")


# ── commands ─────────────────────────────────────────────────────────────────
def cmd_backfill(args) -> None:
    start = date.fromisoformat(args.start)
    end = last_anchor(date.today()) - timedelta(days=7)   # this week is the weekly job's
    have = {s["as_of"] for s in read_csv(SNAPSHOTS) if s["mode"] == "date"}
    todo = [d for d in anchors_between(start, end) if d.isoformat() not in have]
    print(f"Backfill {TECH}: {len(todo)} semanas por bajar ({len(todo)} créditos).")
    if args.dry_run or not todo:
        return
    key = api_key()
    for i, d in enumerate(todo, 1):
        row = snapshot_row("date", d, fetch(key, d))
        append_csv(SNAPSHOTS, SNAP_FIELDS, [row])          # save as we go
        print(f"  [{i}/{len(todo)}] {d}  live={row['live']}")
        time.sleep(REQUEST_PAUSE_S)
    build_dashboard()


def cmd_weekly(args) -> None:
    key = api_key()
    today_anchor = last_anchor(date.today())
    old = read_csv(SNAPSHOTS)
    new_rows = [snapshot_row("current", today_anchor, fetch(key, None))]
    for k in range(0, args.recheck + 1):                 # this week + N previous
        d = today_anchor - timedelta(days=7 * k)
        time.sleep(REQUEST_PAUSE_S)
        new_rows.append(snapshot_row("date", d, fetch(key, d)))
    found = find_restatements(old, new_rows)
    append_csv(SNAPSHOTS, SNAP_FIELDS, new_rows)
    append_csv(RESTATEMENTS, RESTATE_FIELDS, found)
    cur = new_rows[0]
    print(f"Semana {today_anchor}: live={cur['live']}  top1m={cur['top_1m']}  "
          f"créditos usados={len(new_rows)}  restatements={len(found)}")
    for r in found:
        print(f"  RESTATEMENT {r['as_of']} {r['field']}: {r['old_value']} -> {r['new_value']}")
    build_dashboard()


def main() -> None:
    p = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    sub = p.add_subparsers(dest="cmd", required=True)
    b = sub.add_parser("backfill")
    b.add_argument("--start", default=DEFAULT_START)
    b.add_argument("--dry-run", action="store_true")
    w = sub.add_parser("weekly")
    w.add_argument("--recheck", type=int, default=4)
    sub.add_parser("build")
    args = p.parse_args()
    {"backfill": cmd_backfill, "weekly": cmd_weekly, "build": lambda a: build_dashboard()}[args.cmd](args)


if __name__ == "__main__":
    main()

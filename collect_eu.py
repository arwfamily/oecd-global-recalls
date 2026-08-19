#!/usr/bin/env python3
"""
EU collector: Safety Gate weekly-report XML.

Source (verified live 2026-08-19, no auth, report 520 = 2013 week 5):
  https://ec.europa.eu/safety-gate-alerts/api/download/weeklyReport/detail/xml/{id}?language=en

Reports are numbered sequentially, so the same walk is both the daily sync
and the 2005-onward backfill: from the highest id we know, probe UPWARD for
new weeks first (so a fresh Friday report always lands even mid-backfill),
then fill DOWNWARD into history. A direction is closed after
MISS_LIMIT consecutive empty/failed ids. Per-run budget keeps each run
polite; state in eu_state.json makes every run resume where the last
stopped. Records merge into data/canonical/eu.jsonl keyed on id
(EU:{caseNumber}), preserving first_seen — one alert appearing in two
reports (addenda happen) stays one record.

Every network step is optional: a failed fetch closes that direction for
this run and keeps everything already stored.
"""

import datetime as dt
import gzip
import json
import sys
import urllib.request
import xml.etree.ElementTree as ET
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))
from adapters.eu import to_canonical  # noqa: E402

# The `search` parameter is REQUIRED — the server answers 400 without it.
# This is exactly the URL shape verified live (report 520); it was "simplified"
# once by dropping search= and the runner got 400 on every request. Never
# simplify a verified URL without re-verifying the simplification.
URL = ("https://ec.europa.eu/safety-gate-alerts/api/download/"
       "weeklyReport/detail/xml/{rid}"
       "?language=en&search=WEB_REPORT%7C:%7C{rid}")

ROOT = Path(__file__).resolve().parent
RAW_DIR = ROOT / "data" / "raw" / "eu"
OUT = ROOT / "data" / "canonical" / "eu.jsonl"
STATE = ROOT / "data" / "eu_state.json"

SEED = 520          # verified: 2013 week 5
BUDGET = 80         # weekly reports fetched per run (~20 runs for full history)
MISS_LIMIT = 8       # consecutive misses that stop the upward probe this run
MISS_LIMIT_DOWN = 15 # history may contain id gaps; longer fuse before declaring done
FLOOR = 1           # never probe below this id


_FIRST_ERROR = []


def fetch_report(rid):
    """Parsed (root, raw_bytes) or (None, None) on any failure."""
    req = urllib.request.Request(
        URL.format(rid=rid),
        headers={
            # ec.europa.eu rejects non-browser clients from some networks
            # (first run on GitHub's runner fetched 0 of 95 reports) — the
            # NHTSA/Akamai lesson again, so identify as a browser.
            "User-Agent": ("Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) "
                           "AppleWebKit/605.1.15 (KHTML, like Gecko) "
                           "Version/17.4 Safari/605.1.15"),
            "Accept": "application/xml,text/xml;q=0.9,*/*;q=0.8",
            "Accept-Language": "en",
        })
    try:
        with urllib.request.urlopen(req, timeout=60) as resp:
            raw = resp.read()
        root = ET.fromstring(raw)
        if root.tag != "Safety-Gate":
            return None, None
        return root, raw
    except Exception as e:
        if not _FIRST_ERROR:
            _FIRST_ERROR.append(f"report {rid}: {e!r}")
        return None, None


def load_existing():
    records = {}
    if OUT.exists():
        with OUT.open() as f:
            for line in f:
                line = line.strip()
                if line:
                    rec = json.loads(line)
                    records[rec["id"]] = rec
    return records


def load_state():
    try:
        return json.loads(STATE.read_text())
    except Exception:
        # low/high = the CONTIGUOUS done-range; everything outside is未走
        return {"low": SEED, "high": SEED - 1, "low_done": False, "high_done": False}


def ingest(root, rid, existing, today):
    meta = {
        "id": rid,
        "date": (root.findtext("report_date") or "").strip(),
        "year": (root.findtext("report_year") or "").strip(),
        "week": (root.findtext("report_week") or "").strip(),
    }
    added = updated = 0
    for el in root.findall("notifications"):
        rec = to_canonical(el, meta, today)
        if rec["id"] == "EU:":
            continue  # a notification with no case number identifies nothing
        old = existing.get(rec["id"])
        if old is None:
            existing[rec["id"]] = rec
            added += 1
        else:
            rec["first_seen"] = old.get("first_seen", today)
            if rec != old:
                existing[rec["id"]] = rec
                updated += 1
    return added, updated


def main():
    today = dt.date.today().isoformat()
    existing = load_existing()
    state = load_state()
    state_before = dict(state)
    before = len(existing)

    budget = BUDGET
    fetched = added = updated = 0
    any_success = False

    # 1) upward first — new Friday reports beat old history
    misses = 0
    rid = state["high"] + 1
    while budget > 0 and misses < MISS_LIMIT:
        root, raw = fetch_report(rid)
        budget -= 1
        fetched += 1
        if root is None:
            misses += 1
        else:
            misses = 0
            any_success = True
            a, u = ingest(root, rid, existing, today)
            added += a
            updated += u
            RAW_DIR.mkdir(parents=True, exist_ok=True)
            with gzip.open(RAW_DIR / f"report_{rid}.xml.gz", "wb") as f:
                f.write(raw)
            state["high"] = rid
        rid += 1
    # misses at the top are expected (this week's report may not exist yet);
    # they never mark the upward direction done.

    # 2) downward into history
    if not state.get("low_done"):
        misses = 0
        rid = state["low"] - 1
        while budget > 0 and misses < MISS_LIMIT_DOWN and rid >= FLOOR:
            root, raw = fetch_report(rid)
            budget -= 1
            fetched += 1
            if root is None:
                misses += 1
            else:
                misses = 0
                a, u = ingest(root, rid, existing, today)
                added += a
                updated += u
                RAW_DIR.mkdir(parents=True, exist_ok=True)
                with gzip.open(RAW_DIR / f"report_{rid}.xml.gz", "wb") as f:
                    f.write(raw)
            state["low"] = rid
            rid -= 1
        if misses >= MISS_LIMIT_DOWN or rid < FLOOR:
            state["low_done"] = True

    OUT.parent.mkdir(parents=True, exist_ok=True)
    ordered = sorted(existing.values(),
                     key=lambda r: (r.get("date_published") or "", r["id"]),
                     reverse=True)
    tmp = OUT.with_suffix(".tmp")
    with tmp.open("w") as f:
        for rec in ordered:
            f.write(json.dumps(rec, ensure_ascii=False) + "\n")
    tmp.replace(OUT)
    if _FIRST_ERROR and not any_success:
        print(f"  !! every fetch failed — first error: {_FIRST_ERROR[0]}")
    if fetched > 0 and not any_success:
        # network was down or the endpoint changed — moving the pointers now
        # would permanently skip real reports, so keep last run's state
        state = state_before
    STATE.write_text(json.dumps(state))

    # Summary, counted from the written file (operating rule #2)
    n = sum(1 for _ in OUT.open())
    by_type, by_country, by_risk = {}, {}, {}
    for rec in existing.values():
        by_type[rec["record_type"]] = by_type.get(rec["record_type"], 0) + 1
        by_country[rec["authority"]] = by_country.get(rec["authority"], 0) + 1
        for h in rec["hazards"]:
            by_risk[h] = by_risk.get(h, 0) + 1
    print(f"[collect_eu] {today}: {before} -> {n} (+{added}, ~{updated}); "
          f"{fetched} reports fetched, range {state['low']}..{state['high']}, "
          f"backfill {'DONE' if state.get('low_done') else 'in progress'}")
    print("  by type:", dict(sorted(by_type.items(), key=lambda kv: -kv[1])))
    print("  top notifying countries:",
          dict(sorted(by_country.items(), key=lambda kv: -kv[1])[:8]))
    print("  top risk types:",
          dict(sorted(by_risk.items(), key=lambda kv: -kv[1])[:8]))


if __name__ == "__main__":
    main()

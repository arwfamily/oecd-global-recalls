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

# The report-id space has TWO segments (found by hitting the wall between
# them: ids 541-548 all miss while 469-540 and 10000012 exist):
#   legacy  ~1..~540      old RAPEX portal, verified 520 = 2013 week 5
#   modern  10000001..    relaunched portal, verified 10000012 = 2020-09-18
# Each segment is walked independently; priority per run is modern-up (this
# week's report), modern-down, legacy-down (2005-2012 history), legacy-up
# (hole-hunting past 540, bounded so it can never wander toward 10M).
SEGMENTS = {
    "modern": {"seed": 10000012, "floor": 10000001, "ceiling": None,
               "miss_up": 8, "miss_down": 15},
    "legacy": {"seed": 520, "floor": 1, "ceiling": 2000,
               "miss_up": 25, "miss_down": 15},
}
WALK_ORDER = [("modern", "up"), ("modern", "down"),
              ("legacy", "down"), ("legacy", "up")]
BUDGET = 80         # weekly reports fetched per run


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
        s = json.loads(STATE.read_text())
    except Exception:
        s = {}
    if "segments" not in s:
        segs = {}
        for name, cfg in SEGMENTS.items():
            segs[name] = {"low": cfg["seed"], "high": cfg["seed"] - 1,
                          "low_done": False, "high_done": False}
        # migrate a v1 state file (single legacy walk) if one exists
        if "low" in s:
            segs["legacy"] = {"low": s["low"], "high": s["high"],
                              "low_done": s.get("low_done", False),
                              "high_done": False}
        s = {"segments": segs}
    return s


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

    def walk(seg_name, direction):
        nonlocal budget, fetched, added, updated, any_success
        cfg = SEGMENTS[seg_name]
        seg = state["segments"][seg_name]
        if direction == "down" and seg.get("low_done"):
            return
        if direction == "up" and seg.get("high_done"):
            return
        misses = 0
        fuse = cfg["miss_up"] if direction == "up" else cfg["miss_down"]
        rid = seg["high"] + 1 if direction == "up" else seg["low"] - 1
        while budget > 0 and misses < fuse:
            if direction == "down" and rid < cfg["floor"]:
                seg["low_done"] = True
                return
            if direction == "up" and cfg["ceiling"] and rid > cfg["ceiling"]:
                return
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
                if direction == "up":
                    seg["high"] = rid
            if direction == "down":
                seg["low"] = rid
                rid -= 1
            else:
                rid += 1
        if direction == "down" and misses >= fuse:
            seg["low_done"] = True
        # A bounded segment (legacy) is historical: fuse blown upward = top
        # found, close it. An unbounded segment (modern) never closes upward —
        # this week's report simply may not exist yet.
        if direction == "up" and cfg["ceiling"] and (misses >= fuse or rid > cfg["ceiling"]):
            seg["high_done"] = True

    for seg_name, direction in WALK_ORDER:
        walk(seg_name, direction)

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
    rng = " | ".join(
        f"{k} {v['low']}..{v['high']}{' done' if v.get('low_done') else ''}"
        for k, v in state["segments"].items())
    print(f"[collect_eu] {today}: {before} -> {n} (+{added}, ~{updated}); "
          f"{fetched} reports fetched; {rng}")
    print("  by type:", dict(sorted(by_type.items(), key=lambda kv: -kv[1])))
    print("  top notifying countries:",
          dict(sorted(by_country.items(), key=lambda kv: -kv[1])[:8]))
    print("  top risk types:",
          dict(sorted(by_risk.items(), key=lambda kv: -kv[1])[:8]))


if __name__ == "__main__":
    main()


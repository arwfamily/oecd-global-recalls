#!/usr/bin/env python3
"""
OECD GlobalRecalls HISTORICAL BACKFILL (2000 -> today).

Why this exists
---------------
collect_oecd.py can only see the portal's 7-day rolling RSS window.
But the portal's own SPA uses an internal search endpoint (captured by
Angela via F12; public, no auth, CORS *) that accepts date-range queries
back to 2000:

    https://globalrecalls.oecd.org/ws/search.xqy
        ?q=<term> date GE <YYYY-MM-DD> AND date LE <YYYY-MM-DD>
        &start=<offset>&end=<offset+page>&sort=date

CONSTITUTION Art.1: that query shape is the VERIFIED ORIGINAL. Do not
"simplify" it (do not drop the date clause, do not reorder params) without
re-verifying against the live endpoint. The Safety Gate `search` param
incident (every request -> 400) is why.

This one file covers AU (383 baby) and GB (239 baby) — the two
jurisdictions we decided NOT to build native collectors for — and, as a
bonus, gives KR interim coverage (102 baby records) until the native KR
collector (본체 4호) is built. OECD records have no hazard/action fields,
so KR 본체 is still needed for real depth; this is breadth, not depth.

Modes
-----
1. termless (preferred): q = "date GE X AND date LE Y" with no search
   term -> everything in the window. First run logs whether the endpoint
   accepts this ("[mode] termless OK" / "termless rejected -> term mode").
2. term fallback: if termless returns nothing/errors, iterate FALLBACK_TERMS
   per year window. Coarser, but still no ingest-time filtering beyond
   what the endpoint forces on us (CPSC lesson: filtering at ingest
   creates silent false negatives — so the term list is broad, not "baby").

Run model (GitHub Actions friendly)
-----------------------------------
- Budget of REQUEST_BUDGET HTTP calls per run; safe to run twice daily
  alongside the antenna, or via workflow_dispatch until finished.
- Checkpoint: data/oecd_backfill_state.json  — INSIDE data/ on purpose,
  so the workflow's `git add data/` commits it (Constitution Art.3:
  the root-level eu_state.json near-miss).
- No-success guard (Constitution Art.4): if a run gets ZERO successful
  responses, the checkpoint is NOT advanced — a network blackout must
  never permanently skip a year window.
- First error of each run is logged verbatim (Constitution Art.5:
  no guess-fixes; let the next run's log prove the cause).
- Raw response snapshots -> data/raw/oecd_backfill/*.json.gz (L0, immutable).
- Final counts are printed FROM THE WRITTEN FILE, not from run tallies
  (Angela's operating rule).

Output
------
Merges into the SAME data/recalls_global.jsonl the antenna writes, with
the SAME dedup key (country, native_id) and EN-over-FR upgrade — by
importing collect_oecd's own functions, so the two writers can never
drift. Extra search-API treasures are preserved when present:
ext_url (origin-agency per-record link — covers bulk-less AU),
tags (unified classification, e.g. P995=baby, P751=phthalate), made_in.

Usage
-----
    python3 backfill_oecd.py             # one budgeted run
    python3 backfill_oecd.py saved.json  # local test: parse a saved response
Add to collect.yml as a step after collect_oecd, or run via
workflow_dispatch. When finished it prints "[backfill_oecd] FINISHED"
and becomes a cheap no-op.
"""

import datetime as dt
import gzip
import json
import sys
import time
import urllib.error
import urllib.parse
import urllib.request
import xml.etree.ElementTree as ET
from pathlib import Path

# Reuse the antenna's exact parsing/merge/write logic — single source of truth.
from collect_oecd import (  # noqa: E402
    OUT_PATH,
    load_existing,
    merge,
    parse_guid,
    strip_html,
    summarize,
    write_out,
)

BASE_URL = "https://globalrecalls.oecd.org/ws/search.xqy"

ROOT = Path(__file__).resolve().parent
STATE_PATH = ROOT / "data" / "oecd_backfill_state.json"   # inside data/ (Art.3)
RAW_DIR = ROOT / "data" / "raw" / "oecd_backfill"

FIRST_YEAR = 2000
PAGE_SIZE = 50
REQUEST_BUDGET = 100          # HTTP calls per run
POLITE_DELAY_S = 1.0          # be a good citizen on a public endpoint
TIMEOUT_S = 90                # endpoint is slow for non-browser clients

# Constitution Art.2: governments/CDNs block non-browser clients.
HEADERS = {
    "User-Agent": ("Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) "
                   "AppleWebKit/537.36 (KHTML, like Gecko) "
                   "Chrome/126.0.0.0 Safari/537.36"),
    "Accept": "application/json, text/plain, */*",
    "Accept-Language": "en",
    "Referer": "https://globalrecalls.oecd.org/",
}

# Term-mode fallback only. Broad product-space tokens, NOT a baby filter —
# ingest-time filtering is banned (CPSC false-negative lesson).
FALLBACK_TERMS = [
    "a", "e", "i", "o", "u",  # vowel sweep: cheap near-total coverage
    "toy", "baby", "child", "car", "battery", "electric", "food",
]


# ----------------------------------------------------------------- state ---

def load_state() -> dict:
    if STATE_PATH.exists():
        return json.loads(STATE_PATH.read_text())
    return {
        "v": 1,
        "mode": None,              # decided on first successful probe
        "year": dt.date.today().year,  # walk DESCENDING: recent first
        "offset": 0,
        "term_index": 0,
        "finished": False,
    }


def save_state(state: dict) -> None:
    STATE_PATH.parent.mkdir(parents=True, exist_ok=True)
    tmp = STATE_PATH.with_suffix(".tmp")
    tmp.write_text(json.dumps(state, indent=2) + "\n")
    tmp.replace(STATE_PATH)


# ------------------------------------------------------------------ http ---

def build_url(year: int, offset: int, term: str | None) -> str:
    lo, hi = f"{year}-01-01", f"{year}-12-31"
    clause = f"date GE {lo} AND date LE {hi}"
    q = f"{term} {clause}" if term else clause
    # Verified-original param order and shape (Art.1). quote() keeps spaces
    # as %20 exactly as the browser sent them.
    return (f"{BASE_URL}?q={urllib.parse.quote(q)}"
            f"&start={offset}&end={offset + PAGE_SIZE}&sort=date")


def fetch(url: str) -> bytes:
    req = urllib.request.Request(url, headers=HEADERS)
    with urllib.request.urlopen(req, timeout=TIMEOUT_S) as resp:
        return resp.read()


def snapshot_raw(body: bytes, year: int, offset: int, term: str | None):
    RAW_DIR.mkdir(parents=True, exist_ok=True)
    tag = term or "termless"
    path = RAW_DIR / f"{year}_{tag}_{offset}.json.gz"
    with gzip.open(path, "wb") as f:
        f.write(body)


# ----------------------------------------------------------------- parse ---

def _first(d: dict, *keys, default=""):
    for k in keys:
        if k in d and d[k] not in (None, ""):
            return d[k]
    return default


def _items_from_json(doc):
    if isinstance(doc, list):
        return doc
    if isinstance(doc, dict):
        for k in ("results", "items", "recalls", "hits", "docs", "records"):
            v = doc.get(k)
            if isinstance(v, list):
                return v
            if isinstance(v, dict) and isinstance(v.get("hits"), list):
                return v["hits"]
    return []


def _items_from_xml(body: bytes):
    root = ET.fromstring(body)
    out = []
    for item in root.iter():
        if item.tag.split("}")[-1] in ("item", "recall", "result"):
            rec = {child.tag.split("}")[-1]: (child.text or "").strip()
                   for child in item}
            if rec:
                out.append(rec)
    return out


def parse_body(body: bytes):
    """JSON preferred; XML tolerated. Raw is snapshotted either way, so if
    a shape surprises us the NEXT run's log + snapshot settle it (Art.5)."""
    text = body.decode("utf-8", errors="replace").lstrip()
    if text.startswith(("{", "[")):
        return _items_from_json(json.loads(text))
    return _items_from_xml(body)


def normalize(raw: dict):
    """Map a search-API item onto the antenna record shape."""
    guid = str(_first(raw, "guid", "uri", "recallURI", "id"))
    lang, country, native_id = parse_guid(guid)
    if not country:
        return None
    rec = {
        "guid": guid,
        "lang": lang,
        "country": country,
        "country_name": str(_first(raw, "country", "countryName",
                                   "creator", default="")),
        "native_id": native_id,
        "title": strip_html(str(_first(raw, "title", "productName",
                                       "product"))),
        "description": strip_html(str(_first(raw, "description", "summary",
                                             "details"))),
        "date_submitted": str(_first(raw, "dateSubmitted", "date_submitted",
                                     "date"))[:10],
        "portal_link": str(_first(raw, "link", "portalLink", "url")),
    }
    # Treasures unique to the search API — keep them when present.
    ext = _first(raw, "extUrl", "exturl", "ext_url", default=None)
    if ext:
        rec["ext_url"] = str(ext)
    tags = raw.get("tags")
    if tags:
        rec["tags"] = tags if isinstance(tags, list) else [str(tags)]
    made = _first(raw, "made_in", "madeIn", default=None)
    if made:
        rec["made_in"] = str(made)
    return rec


# ------------------------------------------------------------------ main ---

def main():
    today = dt.date.today().isoformat()

    if len(sys.argv) > 1:  # local test on a saved response body
        body = Path(sys.argv[1]).read_bytes()
        items = parse_body(body)
        recs = [r for r in (normalize(i) for i in items) if r]
        print(f"[backfill_oecd] local parse: {len(items)} items "
              f"-> {len(recs)} records")
        for r in recs[:5]:
            print("  ", r["country"], r["date_submitted"], r["title"][:70])
        return

    state = load_state()
    if state.get("finished"):
        print("[backfill_oecd] FINISHED — nothing to do.")
        return

    existing = load_existing()
    before = len(existing)
    budget = REQUEST_BUDGET
    successes = 0
    first_error = None
    total_added = total_updated = 0

    while budget > 0 and not state["finished"]:
        term = None
        if state["mode"] == "term":
            term = FALLBACK_TERMS[state["term_index"]]
        url = build_url(state["year"], state["offset"], term)

        budget -= 1
        try:
            body = fetch(url)
        except (urllib.error.URLError, urllib.error.HTTPError,
                TimeoutError, OSError) as e:
            if first_error is None:
                first_error = f"{type(e).__name__}: {e} @ {url}"
            # mode probe: termless failing hard on the very first call
            if state["mode"] is None:
                print("[mode] termless rejected -> term mode")
                state["mode"] = "term"
                continue
            time.sleep(POLITE_DELAY_S)
            continue

        successes += 1
        snapshot_raw(body, state["year"], state["offset"], term)

        try:
            items = parse_body(body)
        except Exception as e:  # noqa: BLE001 — log first, fix from log
            if first_error is None:
                first_error = f"parse {type(e).__name__}: {e} @ {url}"
            items = []

        if state["mode"] is None:
            state["mode"] = "termless" if items else "term"
            print(f"[mode] probe result: {state['mode']}"
                  + (" OK" if items else " (termless empty -> term mode)"))
            if state["mode"] == "term":
                continue

        recs = [r for r in (normalize(i) for i in items) if r]
        added, updated = merge(existing, recs, today)
        total_added += added
        total_updated += updated

        # advance cursor
        if len(items) >= PAGE_SIZE:
            state["offset"] += PAGE_SIZE
        else:  # window exhausted
            state["offset"] = 0
            if state["mode"] == "term":
                state["term_index"] += 1
                if state["term_index"] < len(FALLBACK_TERMS):
                    continue
                state["term_index"] = 0
            state["year"] -= 1
            if state["year"] < FIRST_YEAR:
                state["finished"] = True

        time.sleep(POLITE_DELAY_S)

    write_out(existing)

    # No-success guard (Art.4): a blackout run must not move the pointer.
    if successes > 0:
        save_state(state)
    else:
        print("[guard] zero successful responses — state NOT advanced")

    if first_error:
        print(f"[first-error] {first_error}")

    # Counts from the written file (Angela's rule), not from run tallies.
    written = load_existing()
    print(f"[backfill_oecd] {today}: {before} -> {len(written)} "
          f"(+{total_added} new, {total_updated} updated) "
          f"mode={state['mode']} cursor={state['year']}/{state['offset']} "
          f"finished={state['finished']}")
    print(summarize(written, today))


if __name__ == "__main__":
    main()

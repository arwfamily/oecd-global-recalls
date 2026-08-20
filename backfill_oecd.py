"""
OECD GlobalRecalls — full-history backfill via the portal's search API.

The RSS feed (collect_oecd.py) is a rolling ~7-day window: it can only move
forward. The portal's own search screen, however, is backed by a public JSON
service that queries the ENTIRE registry back to 2000:

    https://globalrecalls.oecd.org/ws/search.xqy
        ?q=<term?> date GE YYYY-MM-DD AND date LE YYYY-MM-DD
        &start=<offset>&end=<offset+page>&sort=date&order=asc
        &lang=en&uiLang=en

Verified live on 2026-08-19 (captured from the portal via browser dev tools,
CORS header `Access-Control-Allow-Origin: *`, no auth, no cookies needed).
Response echoes `total`, `page-length`, `where`, and returns `results[]` with:

    countryId / countryName   ISO code + display name
    id                        the source agency's own id  -> native_id
    date                      recall date (YYYY-MM-DD)    -> date_submitted
    product.name              title
    extUrl                    THE ORIGINAL REGULATOR URL (cpsc.gov,
                              healthycanadians.gc.ca, recalls.gov.au, ...)
    tags[]                    OECD's cross-country product taxonomy
                              (e.g. #P995 "baby", #P751 "phthalate")
    manufacturer.country[]    where the product was made
    imageUri / languageId

Two things the RSS records never had — extUrl and tags — make backfilled
records RICHER than feed records, so the merge below also enriches existing
feed records in place.

Strategy
--------
- One query window per year, 2000..current. Small result sets per window,
  shallow pagination, resumable per-window.
- Query mode is detected at runtime: we first try a termless date-only query
  (`q=date GE ... AND date LE ...`). If the service rejects it or returns
  zero while the portal clearly has data, we fall back to a sweep of
  child-domain terms (dedup makes the union safe). The run log states which
  mode ran — read it.
- Dedup key is (country, native_id), same as collect_oecd.py. EN beats other
  languages for the same key. Records already in recalls_global.jsonl keep
  their first_seen; their empty ext_url/tags fields are filled in.
- `collected_via` records provenance: "rss" (default for old records),
  "backfill". first_seen for backfilled records is the backfill run date —
  it is truthfully the first day WE saw them; date_submitted carries the
  recall's real date, which is the field to filter on downstream.

Run:  python backfill_oecd.py            # full 2000..today
      python backfill_oecd.py 2019 2022  # just those years (resume/repair)

Politeness: 0.4 s between requests, 3 retries with backoff, custom UA.
State: data/backfill_state.json marks completed windows so a re-run skips
them; delete a year from it (or the file) to force re-collection.
"""

import datetime
import json
import sys
import time
import urllib.parse
import urllib.request
from pathlib import Path

BASE = "https://globalrecalls.oecd.org/ws/search.xqy"
UA = "TinySafe-GlobalRecalls/1.0 (baby product safety; contact: arwfamily)"
OUT_PATH = Path(__file__).parent / "data" / "recalls_global.jsonl"
STATE_PATH = Path(__file__).parent / "data" / "backfill_state.json"
PAGE = 100          # requested page size; server may clamp — we follow its echo
SLEEP = 0.4         # seconds between requests
FALLBACK_TERMS = [  # used only if termless queries are rejected
    "baby", "infant", "toddler", "child", "children", "kids", "nursery",
    "crib", "cot", "bassinet", "stroller", "pram", "pushchair", "walker",
    "high chair", "car seat", "booster", "pacifier", "teether", "teething",
    "toy", "playpen", "sleepwear", "pyjama", "pajama", "bib", "swing",
    "bouncer", "carrier", "sling", "monitor", "bottle", "soother",
]


def http_json(url: str, tries: int = 3):
    for attempt in range(tries):
        try:
            req = urllib.request.Request(url, headers={"User-Agent": UA})
            with urllib.request.urlopen(req, timeout=45) as r:
                return json.loads(r.read().decode("utf-8"))
        except Exception as e:  # noqa: BLE001 — log and retry, network is dirty
            print(f"  [!] attempt {attempt + 1}: {e}", file=sys.stderr)
            time.sleep(2 * (attempt + 1))
    return None


def search(q: str, start: int, end: int):
    params = urllib.parse.urlencode({
        "q": q, "start": start, "end": end,
        "sort": "date", "order": "asc", "lang": "en", "uiLang": "en",
    })
    return http_json(f"{BASE}?{params}")


def window_query(term: str | None, y0: str, y1: str) -> str:
    date_clause = f"date GE {y0} AND date LE {y1}"
    return f"{term} {date_clause}" if term else date_clause


def detect_termless(year: int) -> bool:
    """True if the API accepts a date-only query (no search term)."""
    q = window_query(None, f"{year}-01-01", f"{year}-12-31")
    data = search(q, 0, 1)
    if not data:
        return False
    # The service echoes the where-clause it actually ran. If it ran our
    # date-only clause and reports a sane total, termless mode works.
    ran = str(data.get("where") or "")
    total = data.get("total") or 0
    ok = "date GE" in ran and "AND date LE" in ran and total > 0
    print(f"[*] termless probe ({year}): where={ran!r} total={total} -> "
          f"{'OK' if ok else 'NOT SUPPORTED'}")
    return ok


def norm_record(item: dict, today: str) -> dict | None:
    country = (item.get("countryId") or "").strip()
    native_id = str(item.get("id") or "").strip()
    if not country or not native_id:
        return None
    tags = [
        {"code": str(t.get("name") or "").rsplit("#", 1)[-1],
         "value": (t.get("value") or "").strip()}
        for t in (item.get("tags") or [])
        if isinstance(t, dict) and t.get("value")
    ]
    made_in = [
        (c.get("id") or "").strip()
        for c in (item.get("manufacturer.country") or [])
        if isinstance(c, dict) and c.get("id")
    ]
    lang = (item.get("languageId") or "").strip().upper()
    return {
        "guid": (item.get("uri") or "").strip(),
        "lang": lang,
        "country": country,
        "country_name": (item.get("countryName") or "").strip(),
        "native_id": native_id,
        "title": (item.get("product.name") or "").strip(),
        "description": "",  # the search API returns no description body
        "date_submitted": (item.get("date") or "").strip(),
        "portal_link": (item.get("uri") or "").strip(),
        "ext_url": (item.get("extUrl") or "").strip(),
        "image_url": (item.get("imageUri") or "").strip() or None,
        "tags": tags,
        "made_in": made_in,
        "first_seen": today,
        "collected_via": "backfill",
    }


def load_existing() -> dict:
    records = {}
    if OUT_PATH.exists():
        with OUT_PATH.open() as f:
            for line in f:
                line = line.strip()
                if line:
                    rec = json.loads(line)
                    records[(rec["country"], rec["native_id"])] = rec
    return records


def merge(records: dict, rec: dict) -> str:
    """Merge one backfilled record into the store. Returns what happened."""
    key = (rec["country"], rec["native_id"])
    old = records.get(key)
    if old is None:
        records[key] = rec
        return "new"
    # Same recall already known (from RSS or an earlier window).
    # EN beats other languages for the text fields; enrichment fields
    # (ext_url, tags, made_in, image_url) fill in wherever they are empty.
    changed = False
    if rec["lang"] == "EN" and old.get("lang") != "EN":
        # EN wins the human-facing fields — including ext_url, because that
        # is the link a parent clicks (the FR twin points at the -fra page).
        for k in ("lang", "title", "guid", "portal_link", "country_name",
                  "ext_url", "image_url"):
            if rec.get(k):
                old[k] = rec[k]
                changed = True
    for k in ("ext_url", "image_url", "made_in", "date_submitted"):
        if rec.get(k) and not old.get(k):
            old[k] = rec[k]
            changed = True
    # Tags are a union by taxonomy code: the EN and FR twins of one recall
    # can carry different subsets, and the codes are language-independent.
    if rec.get("tags"):
        have = {t.get("code") for t in (old.get("tags") or [])}
        add = [t for t in rec["tags"] if t.get("code") not in have]
        if add:
            old["tags"] = (old.get("tags") or []) + add
            changed = True
    old.setdefault("collected_via", "rss")
    return "enriched" if changed else "dup"


def collect_window(records: dict, term: str | None, y0: str, y1: str,
                   today: str) -> dict:
    q = window_query(term, y0, y1)
    counts = {"new": 0, "enriched": 0, "dup": 0}
    start, total = 0, None
    while True:
        data = search(q, start, start + PAGE)
        if data is None:
            print(f"  [!] window {y0}..{y1} term={term!r}: giving up at "
                  f"offset {start} — re-run to resume", file=sys.stderr)
            break
        if total is None:
            total = int(data.get("total") or 0)
        results = data.get("results") or []
        if not results:
            break
        for item in results:
            rec = norm_record(item, today)
            if rec:
                counts[merge(records, rec)] += 1
        start += len(results)
        if start >= total:
            break
        time.sleep(SLEEP)
    counts["total_reported"] = total or 0
    return counts


def save(records: dict):
    OUT_PATH.parent.mkdir(parents=True, exist_ok=True)
    rows = sorted(records.values(),
                  key=lambda r: (r.get("date_submitted") or "",
                                 r.get("country") or ""))
    tmp = OUT_PATH.with_suffix(".tmp")
    with tmp.open("w") as f:
        for r in rows:
            f.write(json.dumps(r, ensure_ascii=False) + "\n")
    tmp.replace(OUT_PATH)


def main():
    today = datetime.date.today().isoformat()
    this_year = datetime.date.today().year
    y_from = int(sys.argv[1]) if len(sys.argv) > 1 else 2000
    y_to = int(sys.argv[2]) if len(sys.argv) > 2 else this_year

    state = {}
    if STATE_PATH.exists():
        state = json.loads(STATE_PATH.read_text())

    records = load_existing()
    print(f"[*] existing store: {len(records)} records")

    termless = detect_termless(min(2015, this_year))
    if not termless:
        print(f"[*] falling back to a {len(FALLBACK_TERMS)}-term sweep per "
              f"year window (dedup makes the union safe)")

    grand = {"new": 0, "enriched": 0, "dup": 0}
    for year in range(y_from, y_to + 1):
        wkey = f"{year}:{'termless' if termless else 'sweep'}"
        if state.get(wkey) == "done":
            print(f"[{year}] already done — skipping (state file)")
            continue
        y0, y1 = f"{year}-01-01", f"{year}-12-31"
        if termless:
            c = collect_window(records, None, y0, y1, today)
            print(f"[{year}] total={c['total_reported']} new={c['new']} "
                  f"enriched={c['enriched']} dup={c['dup']}")
            for k in grand:
                grand[k] += c[k]
        else:
            for term in FALLBACK_TERMS:
                c = collect_window(records, term, y0, y1, today)
                if c["total_reported"]:
                    print(f"[{year}] {term!r}: total={c['total_reported']} "
                          f"new={c['new']} enriched={c['enriched']}")
                for k in grand:
                    grand[k] += c[k]
                time.sleep(SLEEP)
        state[wkey] = "done"
        STATE_PATH.parent.mkdir(parents=True, exist_ok=True)
        STATE_PATH.write_text(json.dumps(state, indent=1))
        save(records)  # checkpoint after every year — a crash loses nothing

    print(f"\n[*] backfill done: +{grand['new']} new, "
          f"{grand['enriched']} enriched, {grand['dup']} already known")
    print(f"[*] store now: {len(records)} records -> {OUT_PATH}")


if __name__ == "__main__":
    main()

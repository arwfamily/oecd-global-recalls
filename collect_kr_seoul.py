"""
KR collector — walk the Seoul mirror's domestic recall list.

Zero-auth door into Korean (safetykorea-origin) recalls. GET pagination,
browser-like headers (EU lesson: gov endpoints reject bare clients),
polite 0.5 s sleep, raw HTML snapshots kept under data/raw/kr_seoul/ (L0),
merged output at data/canonical/kr.jsonl keyed by record id.

The mirror holds ~3 years only, so every run walks ALL pages of the
domestic list (~124 pages ≈ 1 minute) — new records join, known records
refresh, and disappearance from the mirror does NOT delete anything
(the canonical store only grows).

The run log reports how many records carried a real safetykorea uid vs a
natural key — read that line on the first run: it decides whether the
safetykorea Open API is needed for identity or only for backfill.
"""

import datetime
import json
import re
import sys
import time
import urllib.request
from pathlib import Path

from adapters.kr_seoul import parse_list_page

BASE = "https://sftc.seoul.go.kr/fe/si/recallDmstc/NR_list.do"
HEADERS = {
    "User-Agent": ("Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) "
                   "AppleWebKit/605.1.15 (KHTML, like Gecko) Version/17.4 Safari/605.1.15"),
    "Accept": "text/html,application/xhtml+xml,application/xml;q=0.9,*/*;q=0.8",
    "Accept-Language": "ko-KR,ko;q=0.9,en;q=0.5",
}
OUT = Path(__file__).parent / "data" / "canonical" / "kr.jsonl"
RAW_DIR = Path(__file__).parent / "data" / "raw" / "kr_seoul"
SLEEP = 0.5
MAX_PAGES = 400          # fuse; the site reports ~124 today
LAST_PAGE_RE = re.compile(r"pageIndex=(\d+)[^>]*>\s*(?:마지막|Last)", re.I)
TOTAL_RE = re.compile(r"전체\s*([\d,]+)\s*건")


def fetch(page: int, tries: int = 3) -> str:
    url = f"{BASE}?pageIndex={page}"
    for attempt in range(tries):
        try:
            req = urllib.request.Request(url, headers=HEADERS)
            with urllib.request.urlopen(req, timeout=40) as r:
                return r.read().decode("utf-8", errors="replace")
        except Exception as e:  # noqa: BLE001
            print(f"  [!] page {page} attempt {attempt + 1}: {e}", file=sys.stderr)
            time.sleep(2 * (attempt + 1))
    return ""


def main():
    OUT.parent.mkdir(parents=True, exist_ok=True)
    RAW_DIR.mkdir(parents=True, exist_ok=True)
    store = {}
    if OUT.exists():
        with OUT.open() as f:
            for line in f:
                if line.strip():
                    r = json.loads(line)
                    store[r["id"]] = r
    print(f"[*] existing KR store: {len(store)}")

    today = datetime.date.today().isoformat()
    first = fetch(1)
    if not first:
        sys.exit("[!] page 1 unreachable — aborting without touching the store")
    m = TOTAL_RE.search(first)
    lp = LAST_PAGE_RE.search(first)
    last_page = min(int(lp.group(1)) if lp else MAX_PAGES, MAX_PAGES)
    print(f"[*] mirror reports total={m.group(1) if m else '?'} "
          f"last_page={last_page}")
    (RAW_DIR / f"list_p1_{today}.html").write_text(first)  # L0 snapshot

    new = refreshed = uid_count = nk_count = 0
    page = 1
    while page <= last_page:
        html = first if page == 1 else fetch(page)
        if not html:
            print(f"  [!] page {page} failed after retries — continuing")
            page += 1
            continue
        rows = parse_list_page(html, "domestic")
        if not rows and page > 1:
            print(f"  [*] page {page}: no data rows — stopping walk")
            break
        for rec in rows:
            uid_count += rec["id_scheme"] == "safetykorea_uid"
            nk_count += rec["id_scheme"] == "natural_key"
            rec["first_seen"] = store.get(rec["id"], {}).get("first_seen", today)
            rec["last_seen"] = today
            if rec["id"] in store:
                refreshed += 1
            else:
                new += 1
            store[rec["id"]] = rec
        page += 1
        if page <= last_page:
            time.sleep(SLEEP)

    rows_sorted = sorted(store.values(),
                         key=lambda r: (r.get("date_published") or "", r["id"]))
    tmp = OUT.with_suffix(".tmp")
    with tmp.open("w") as f:
        for r in rows_sorted:
            f.write(json.dumps(r, ensure_ascii=False) + "\n")
    tmp.replace(OUT)
    print(f"[*] done: +{new} new, {refreshed} refreshed, store={len(store)}")
    print(f"[*] identity: safetykorea_uid={uid_count} natural_key={nk_count}"
          f"{'  <- uid extraction worked!' if uid_count else ''}")
    if uid_count == 0 and (new or refreshed):
        print("[*] no uids exposed in list HTML — identity is natural-key for "
              "now; the safetykorea Open API upgrade will supply real uids")


if __name__ == "__main__":
    main()

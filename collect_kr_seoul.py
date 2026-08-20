"""
KR collector v2 — walk the Seoul mirror's domestic recall list.

v1 run findings (2026-08-19, live):
- 760/1,240 collected, then page 78 returned no data rows and the walk
  concluded "end of data". 77 rapid cookieless requests then emptiness
  smells like session/rate handling, not the end of the list.
- last-page detection via the '마지막' anchor failed (last_page=400 fuse);
  the '전체 1240 건(1/124 page)' text is the sturdier source.
- No uids in the list HTML — identity stays natural-key until the
  safetykorea Open API supplies real recallUids.

v2 therefore:
1. keeps the JSESSIONID cookie from the first response and replays it
   (eGov sites often demand a live session for deeper pages);
2. reads last_page from '(x/y page)';
3. treats an empty page BEFORE the expected last page as an anomaly:
   waits, retries twice with backoff, snapshots the raw HTML to
   data/raw/kr_seoul/ (L0) so the next session can see what the server
   actually said, and only then skips that page — never silently
   concluding the walk early;
4. slows to 1.2 s between pages;
5. prints the first data row's raw <tr> once per run (truncated) so we
   can inspect the real anchor markup for uid patterns.
"""

import datetime
import json
import re
import sys
import time

sys.stdout.reconfigure(line_buffering=True)  # live logs in Actions
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
SLEEP = 1.2
MAX_PAGES = 400                     # absolute fuse
PAGES_RE = re.compile(r"\(\s*\d+\s*/\s*(\d+)\s*page\s*\)")
TOTAL_RE = re.compile(r"전체\s*([\d,]+)\s*건")
ROW_SNIPPET_RE = re.compile(r"<tr[^>]*>\s*<td[^>]*>\s*\d+\s*</td>.*?</tr>", re.S)

_cookie = {"v": ""}


def fetch(page: int, tries: int = 3) -> str:
    url = f"{BASE}?pageIndex={page}"
    for attempt in range(tries):
        try:
            headers = dict(HEADERS)
            if _cookie["v"]:
                headers["Cookie"] = _cookie["v"]
            req = urllib.request.Request(url, headers=headers)
            with urllib.request.urlopen(req, timeout=40) as r:
                sc = r.headers.get("Set-Cookie")
                if sc and "JSESSIONID" in sc and not _cookie["v"]:
                    _cookie["v"] = sc.split(";", 1)[0]
                    print(f"[*] session cookie captured")
                return r.read().decode("utf-8", errors="replace")
        except Exception as e:  # noqa: BLE001
            print(f"  [!] page {page} attempt {attempt + 1}: {e}", file=sys.stderr)
            time.sleep(3 * (attempt + 1))
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
    total = TOTAL_RE.search(first)
    pages = PAGES_RE.search(first)
    last_page = min(int(pages.group(1)) if pages else MAX_PAGES, MAX_PAGES)
    print(f"[*] mirror reports total={total.group(1) if total else '?'} "
          f"last_page={last_page}"
          f"{'' if pages else '  (page-count text not found — fuse in effect)'}")
    (RAW_DIR / f"list_p1_{today}.html").write_text(first)

    snip = ROW_SNIPPET_RE.search(first)
    if snip:
        print(f"[*] raw first data row (for uid inspection):\n"
              f"    {snip.group(0)[:500]}")

    # Migration index: natural-key records from the pre-uid era get
    # superseded in place the moment the same row arrives carrying a real
    # safetykorea uid — first_seen is carried over, the nk id retires.
    nk_index = {}
    for rid, r in store.items():
        if r.get("id_scheme") == "natural_key":
            nk = (r.get("date_published"), r.get("product_name"),
                  r.get("model"), r.get("company"))
            nk_index[nk] = rid

    new = refreshed = uid_count = nk_count = migrated = 0
    consecutive_empty = 0
    page = 1
    while page <= last_page:
        html = first if page == 1 else fetch(page)
        rows = parse_list_page(html, "domestic") if html else []
        if not rows and page < last_page:
            # v1/v2 lesson (2026-08-19): the site's '전체 N 건' counter is
            # STALE — it counts purged rows too. The displayed window ended
            # at page 77 while the counter promised 124. One quick retry
            # guards against a transient blank; a second consecutive empty
            # page means the display window is over. That is data-end, not
            # an anomaly — stop cleanly instead of grinding backoffs.
            time.sleep(4)
            html = fetch(page)
            rows = parse_list_page(html, "domestic") if html else []
            if not rows:
                consecutive_empty += 1
                if consecutive_empty == 1:
                    (RAW_DIR / f"window_end_p{page}_{today}.html").write_text(html or "")
                if consecutive_empty >= 2:
                    print(f"  [*] pages {page - 1}-{page} empty — display "
                          f"window ends here (site total counter is stale); "
                          f"stopping walk")
                    break
                page += 1
                time.sleep(SLEEP)
                continue
        consecutive_empty = 0
        for rec in rows:
            uid_count += rec["id_scheme"] == "safetykorea_uid"
            nk_count += rec["id_scheme"] == "natural_key"
            first_seen = store.get(rec["id"], {}).get("first_seen")
            if rec["id_scheme"] == "safetykorea_uid" and rec["id"] not in store:
                nk = (rec.get("date_published"), rec.get("product_name"),
                      rec.get("model"), rec.get("company"))
                old_id = nk_index.pop(nk, None)
                if old_id and old_id in store:
                    first_seen = store[old_id].get("first_seen")
                    del store[old_id]          # nk record retires in place
                    migrated += 1
            rec["first_seen"] = first_seen or today
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
    print(f"[*] done: +{new} new, {refreshed} refreshed, "
          f"migrated_nk_to_uid={migrated}, store={len(store)}")
    print(f"[*] identity: safetykorea_uid={uid_count} natural_key={nk_count}")
    exp = int(total.group(1).replace(",", "")) if total else None
    if exp and len(store) < exp:
        print(f"[*] store {len(store)} < mirror total {exp} — "
              f"re-run to fill remaining pages (walk resumes cheaply)")


if __name__ == "__main__":
    main()

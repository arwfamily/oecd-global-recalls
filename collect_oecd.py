#!/usr/bin/env python3
"""
OECD GlobalRecalls daily collector.

The portal's only machine-readable surface is a 7-DAY ROLLING RSS feed:
    https://globalrecalls.oecd.org/ws/rss.xqy
Anything not collected within 7 days of publication is lost (to us).
This script is therefore run daily by GitHub Actions and:

  1. saves the raw XML snapshot to data/raw/YYYY-MM-DD.xml  (audit trail)
  2. parses items and merges them into data/recalls_global.jsonl

Design decisions (see README):
  - NO filtering at collection time. No baby filter, no country filter,
    no date filter. Collection and curation are separate steps; the CPSC
    work proved that filtering at ingest creates silent false negatives.
  - Dedup key is (country, native_id) — language-independent, which
    automatically folds Canada's EN/FR duplicate records into one.
    When both languages exist, the EN record wins.
  - `date_submitted` is the recall's real date. `first_seen` is when WE
    first collected it. The US is currently backfilling year-2000 records
    into the feed, so date_submitted is the field to filter on downstream.
"""

import datetime as dt
import html
import json
import re
import sys
import urllib.request
import xml.etree.ElementTree as ET
from pathlib import Path

FEED_URL = "https://globalrecalls.oecd.org/ws/rss.xqy"
DCTERMS = "{http://purl.org/dc/terms/}"

ROOT = Path(__file__).resolve().parent
RAW_DIR = ROOT / "data" / "raw"
OUT_PATH = ROOT / "data" / "recalls_global.jsonl"

TAG_RE = re.compile(r"<[^>]+>")
WS_RE = re.compile(r"\s+")


def fetch_feed() -> bytes:
    req = urllib.request.Request(
        FEED_URL, headers={"User-Agent": "oecd-global-recalls-collector/1.0"}
    )
    with urllib.request.urlopen(req, timeout=60) as resp:
        return resp.read()


def strip_html(text: str) -> str:
    """Description fields carry escaped HTML (<p>, <ul>...). Flatten to text."""
    if not text:
        return ""
    text = html.unescape(text)
    text = TAG_RE.sub(" ", text)
    return WS_RE.sub(" ", text).strip()


def parse_guid(guid: str):
    """
    guid shape: http://PoliciesApplications.oecd.org/GlobalRecalls/Recall/EN/US/26688
    Language and country are the first two segments after /Recall/; everything
    after them is the origin agency's own id, which may itself contain slashes
    (e.g. Korea: EN/KR/K26/EP/10022792, Bulgaria: EN/BG/SR/03722/25).
    """
    marker = "/Recall/"
    idx = guid.find(marker)
    if idx == -1:
        return None, None, None
    parts = guid[idx + len(marker):].split("/")
    if len(parts) < 3:
        return None, None, None
    lang, country = parts[0].upper(), parts[1].upper()
    native_id = "/".join(parts[2:])
    return lang, country, native_id


def parse_items(xml_bytes: bytes):
    root = ET.fromstring(xml_bytes)
    for item in root.iter("item"):
        guid = (item.findtext("guid") or "").strip()
        lang, country, native_id = parse_guid(guid)
        if not country:
            continue
        yield {
            "guid": guid,
            "lang": lang,
            "country": country,
            "country_name": (item.findtext(f"{DCTERMS}creator") or "").strip(),
            "native_id": native_id,
            "title": (item.findtext("title") or "").strip(),
            "description": strip_html(item.findtext("description") or ""),
            "date_submitted": (item.findtext(f"{DCTERMS}dateSubmitted") or "").strip(),
            "portal_link": (item.findtext("link") or "").strip(),
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


ENRICHED_FIELDS = ("ext_url", "image_url", "tags", "made_in", "collected_via")


def merge(existing: dict, incoming, today: str):
    added = updated = 0
    for rec in incoming:
        key = (rec["country"], rec["native_id"])
        if key not in existing:
            rec["first_seen"] = today
            existing[key] = rec
            added += 1
        else:
            old = existing[key]
            # The backfill (backfill_oecd.py) enriches records with fields
            # the RSS feed never carries — ext_url, taxonomy tags, made_in.
            # Any replacement below must inherit them, or an RSS refresh
            # (FR->EN upgrade, amended text) silently strips a record back
            # down to feed-thin.
            for k in ENRICHED_FIELDS:
                if old.get(k) and not rec.get(k):
                    rec[k] = old[k]
            # Same recall seen again. Upgrade FR -> EN; otherwise refresh
            # text fields in case the source amended them. Keep first_seen.
            if old.get("lang") != "EN" and rec["lang"] == "EN":
                rec["first_seen"] = old.get("first_seen", today)
                existing[key] = rec
                updated += 1
            elif rec["lang"] == old.get("lang") and (
                rec["title"] != old.get("title")
                or rec["description"] != old.get("description")
            ):
                rec["first_seen"] = old.get("first_seen", today)
                existing[key] = rec
                updated += 1
    return added, updated


def write_out(records: dict):
    ordered = sorted(
        records.values(),
        key=lambda r: (r.get("date_submitted") or "", r.get("country") or ""),
        reverse=True,
    )
    tmp = OUT_PATH.with_suffix(".tmp")
    with tmp.open("w") as f:
        for rec in ordered:
            f.write(json.dumps(rec, ensure_ascii=False) + "\n")
    tmp.replace(OUT_PATH)


def summarize(records: dict, today: str):
    by_country = {}
    new_today = 0
    for rec in records.values():
        by_country[rec["country_name"] or rec["country"]] = (
            by_country.get(rec["country_name"] or rec["country"], 0) + 1
        )
        if rec.get("first_seen") == today:
            new_today += 1
    lines = [f"total={len(records)} new_today={new_today}"]
    for name, n in sorted(by_country.items(), key=lambda kv: -kv[1]):
        lines.append(f"  {name}: {n}")
    return "\n".join(lines)


def main():
    today = dt.date.today().isoformat()
    RAW_DIR.mkdir(parents=True, exist_ok=True)

    if len(sys.argv) > 1:  # local test: pass a saved XML file
        xml_bytes = Path(sys.argv[1]).read_bytes()
    else:
        xml_bytes = fetch_feed()
        (RAW_DIR / f"{today}.xml").write_bytes(xml_bytes)

    existing = load_existing()
    before = len(existing)
    added, updated = merge(existing, parse_items(xml_bytes), today)
    write_out(existing)

    print(f"[collect_oecd] {today}: {before} -> {len(existing)} "
          f"(+{added} new, {updated} updated)")
    print(summarize(existing, today))


if __name__ == "__main__":
    main()

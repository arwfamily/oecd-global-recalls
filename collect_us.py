#!/usr/bin/env python3
"""
US collector: tinysafe-data published full record set.

Source: github.com/arwfamily/tinysafe-data — recalls_full.jsonl, the 73-field
build output of the US pipeline (CPSC recalls + safety warnings, FDA
enforcement + press releases, NHTSA child restraints), rebuilt daily by that
repo's own Action. This collector adapts the artifact; it never re-fetches
the agencies, so there is exactly one US pipeline.

Flow:  fetch -> data/raw/us/latest.jsonl.gz (overwritten; canonical layer
preserves history) -> adapt every record -> merge into
data/canonical/us.jsonl keyed on id, preserving first_seen.
"""

import datetime as dt
import gzip
import json
import sys
import urllib.request
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))
from adapters.us import to_canonical  # noqa: E402

BULK_URL = ("https://raw.githubusercontent.com/arwfamily/tinysafe-data/"
            "main/recalls_full.jsonl")

ROOT = Path(__file__).resolve().parent
RAW = ROOT / "data" / "raw" / "us" / "latest.jsonl.gz"
OUT = ROOT / "data" / "canonical" / "us.jsonl"


def fetch() -> bytes:
    req = urllib.request.Request(
        BULK_URL, headers={"User-Agent": "arw-global-recalls-collector/1.0"})
    with urllib.request.urlopen(req, timeout=300) as resp:
        return resp.read()


def load_existing() -> dict:
    records = {}
    if OUT.exists():
        with OUT.open() as f:
            for line in f:
                line = line.strip()
                if line:
                    rec = json.loads(line)
                    records[rec["id"]] = rec
    return records


def main():
    today = dt.date.today().isoformat()

    if len(sys.argv) > 1:  # local test: pass a saved jsonl file
        raw_bytes = Path(sys.argv[1]).read_bytes()
    else:
        raw_bytes = fetch()
        RAW.parent.mkdir(parents=True, exist_ok=True)
        with gzip.open(RAW, "wb") as f:
            f.write(raw_bytes)

    source_records = [json.loads(l) for l in raw_bytes.decode("utf-8").splitlines() if l.strip()]
    existing = load_existing()
    before = len(existing)

    added = updated = 0
    for src in source_records:
        rec = to_canonical(src, today)
        old = existing.get(rec["id"])
        if old is None:
            existing[rec["id"]] = rec
            added += 1
        else:
            rec["first_seen"] = old.get("first_seen", today)
            if rec != old:
                existing[rec["id"]] = rec
                updated += 1

    OUT.parent.mkdir(parents=True, exist_ok=True)
    ordered = sorted(existing.values(),
                     key=lambda r: (r.get("date_published") or "", r["id"]),
                     reverse=True)
    tmp = OUT.with_suffix(".tmp")
    with tmp.open("w") as f:
        for rec in ordered:
            f.write(json.dumps(rec, ensure_ascii=False) + "\n")
    tmp.replace(OUT)

    # Summary, counted from the written file (operating rule #2)
    n = sum(1 for _ in OUT.open())
    by_type, by_auth, joint = {}, {}, 0
    for rec in existing.values():
        by_type[rec["record_type"]] = by_type.get(rec["record_type"], 0) + 1
        by_auth[rec["authority"]] = by_auth.get(rec["authority"], 0) + 1
        joint += bool(rec["joint_with"])
    print(f"[collect_us] {today}: {before} -> {n} (+{added}, ~{updated})")
    print("  by type:", dict(sorted(by_type.items(), key=lambda kv: -kv[1])))
    print("  by authority:", dict(sorted(by_auth.items(), key=lambda kv: -kv[1])))
    print(f"  declared joint-with (Inconjunctions): {joint}")


if __name__ == "__main__":
    main()

"""Adapter: tinysafe-data full record -> canonical record.

Input: one object from recalls_full.jsonl (github.com/arwfamily/tinysafe-data).
The US pipeline already did the hard work — curation, hazard derivation,
text repair — so this adapter maps, it does not re-derive. Contract as in
SCHEMA.md: map what we can, whole original in source_payload, never filter,
never invent ids.

Why the global repo does not fetch CPSC/FDA/NHTSA itself: tinysafe-data is
the single source of truth for US and updates daily. Re-fetching here would
mean two US pipelines that drift; adapting the published artifact means the
US body is exactly what the app already shows.
"""

import re

# The pipeline's source labels vs the origin agency. "FDA datatables
# (press releases)" is our ingest-path label; the agency is FDA.
_AGENCY = {"CPSC": "CPSC", "FDA": "FDA", "NHTSA": "NHTSA"}

# Regulator-declared joint actions. The CPSC API's `Inconjunctions` field
# (carried as inconjunction_urls once the 2026-08-19 merge patch lands)
# names the partner jurisdiction by URL — declared links, no fuzzy matching.
_URL_JURIS = [
    (re.compile(r"canada\.ca|recalls-rappels|healthycanadians", re.I), "CA"),
    (re.compile(r"gov\.uk", re.I), "GB"),
    (re.compile(r"europa\.eu", re.I), "EU"),
    (re.compile(r"productsafety\.gov\.au|accc\.gov\.au", re.I), "AU"),
    (re.compile(r"go\.kr", re.I), "KR"),
    (re.compile(r"profeco|gob\.mx", re.I), "MX"),
]


def _iso(yyyymmdd):
    s = str(yyyymmdd or "").strip()
    if len(s) == 8 and s.isdigit():
        return f"{s[:4]}-{s[4:6]}-{s[6:8]}"
    return ""


def _joint_with(rec):
    urls = rec.get("inconjunction_urls") or []
    if isinstance(urls, str):
        urls = [urls]
    out = []
    for u in urls:
        for pat, j in _URL_JURIS:
            if pat.search(str(u)) and j not in out:
                out.append(j)
    return out


def to_canonical(rec: dict, first_seen: str) -> dict:
    rid = str(rec.get("recall_id", "")).strip()
    src_label = str(rec.get("source", "")).strip()
    agency = _AGENCY.get(src_label.split()[0] if src_label else "", src_label)

    brands = rec.get("brands")
    if not isinstance(brands, list) or not brands:
        b = str(rec.get("brand") or "").strip()
        brands = [b] if b else []

    # tinysafe's vocabulary is recall|warning; canonical is
    # recall|safety_warning|category_alert|advisory.
    rt = rec.get("record_type") or "recall"
    record_type = "safety_warning" if rt == "warning" else rt

    return {
        "id": f"US:{rid}",
        "jurisdiction": "US",
        "source": "tinysafe-us",
        "record_type": record_type,
        "authority": agency,
        "title": rec.get("heading") or rec.get("display_name") or "",
        "product_name": rec.get("product_name") or "",
        "brands": brands,
        "hazard_text": rec.get("hazard_text") or rec.get("reason") or "",
        "hazards": rec.get("hazards") or ([rec["hazard"]] if rec.get("hazard") else []),
        "risk_class_source": str(rec.get("classification") or "").strip(),
        "action_text": rec.get("action") or "",
        # tinysafe's category_family is OUR derivation, not the agency's own
        # label, so it does not belong in category_source. FDA's center-level
        # product_type is the only true source label the store carries.
        "category_source": str(rec.get("product_type") or "").strip(),
        "date_published": _iso(rec.get("recall_date")),
        "date_updated": _iso(rec.get("terminated_date") or ""),
        "first_seen": first_seen,
        "url": rec.get("url") or "",
        "lang": "EN",
        "joint_with": _joint_with(rec),
        "source_payload": rec,
    }

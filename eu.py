"""Adapter: EU Safety Gate weekly-report XML notification -> canonical record.

Input: one <notifications> element from
https://ec.europa.eu/safety-gate-alerts/api/download/weeklyReport/detail/xml/{id}?language=en
(endpoint verified live 2026-08-19 on report 520 = 2013 week 5).

Contract as in SCHEMA.md: map what we can, whole original in
source_payload, never filter, never invent ids.

Quirks observed in the real XML (all from report 520):
- `danger` opens with a literal "null " token on every record — strip it.
- `brand` can carry HTML debris, truncated mid-tag:
  `Polly Pocket <font color="#ff0000">(THE PRODUCT MA` — cut at the first
  '<' and strip tags.
- `type_numberOfModel` can embed <a href> markup (VIN list links).
- Many fields carry the literal string "Unknown" — treated as empty.
- The weekly XML carries no per-alert date; date_published is the REPORT
  date (weekly precision — honest, and refinable later from the alert
  detail page if ever needed).
- `measures` is a concatenated sentence; "Recall of the product from end
  users" marks a consumer recall. Everything else (withdrawal from the
  market, import rejection, empty) is not a consumer recall — the parent
  has nothing to return and nobody will contact them — which is the
  safety_warning shape in our vocabulary.
"""

import html
import re

WS_RE = re.compile(r"\s+")
TAG_RE = re.compile(r"<[^>]*>")
NULL_LEAD = re.compile(r"^\s*null\s+")
RECALL_MEASURE = re.compile(r"recall of the product from end[- ]users?", re.I)


def _clean(text):
    if text is None:
        return ""
    text = html.unescape(str(text))
    text = TAG_RE.sub(" ", text)
    text = WS_RE.sub(" ", text).strip()
    return "" if text == "Unknown" else text


def _brand(text):
    # Debris shows up as a truncated tag; everything before the first '<'
    # is the brand the source meant.
    t = str(text or "").split("<", 1)[0]
    return _clean(t)


def _iso_from_report_date(ddmmyyyy):
    m = re.match(r"^(\d{2})/(\d{2})/(\d{4})$", str(ddmmyyyy or "").strip())
    return f"{m.group(3)}-{m.group(2)}-{m.group(1)}" if m else ""


def _field(el, tag):
    node = el.find(tag)
    return node.text if node is not None else ""


def to_canonical(el, report_meta: dict, first_seen: str) -> dict:
    """el: xml.etree Element for one <notifications>;
    report_meta: {'date': 'DD/MM/YYYY', 'year': ..., 'week': ..., 'id': ...}."""
    case = _clean(_field(el, "caseNumber"))
    danger = NULL_LEAD.sub("", _clean(_field(el, "danger")))
    measures = _clean(_field(el, "measures"))
    risk_types = [r.strip() for r in _clean(_field(el, "riskType")).split(",")
                  if r.strip()]
    brand = _brand(_field(el, "brand"))
    name = _clean(_field(el, "name"))
    product = _clean(_field(el, "product"))
    pictures = [_clean(p.text) for p in el.findall("pictures/picture")
                if _clean(p.text)]

    record_type = "recall" if RECALL_MEASURE.search(measures) else "safety_warning"

    payload = {c.tag: (c.text or "") for c in el if c.tag != "pictures"}
    payload["pictures"] = pictures
    payload["_report"] = dict(report_meta)

    return {
        "id": f"EU:{case}",
        "jurisdiction": "EU",
        "source": "eu-safety-gate",
        "record_type": record_type,
        # The notifying member state IS the acting authority in this system;
        # Safety Gate itself only relays.
        "authority": _clean(_field(el, "notifyingCountry")),
        "title": " — ".join(x for x in (product, brand or None, name or None) if x),
        "product_name": name or product,
        "brands": [brand] if brand else [],
        "hazard_text": danger,
        "hazards": risk_types,
        "risk_class_source": _clean(_field(el, "level")),
        "action_text": measures,
        "category_source": _clean(_field(el, "category")),
        "date_published": _iso_from_report_date(report_meta.get("date")),
        "date_updated": "",
        "first_seen": first_seen,
        "url": _clean(_field(el, "reference")),
        "lang": "EN",
        "joint_with": [],
        "source_payload": payload,
    }

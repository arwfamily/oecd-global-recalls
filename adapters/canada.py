"""Adapter: Canada Recalls & Safety Alerts bulk record -> canonical record.

Input: one object from HCRSAMOpenData.json
Contract: map what we can, put the whole original in source_payload,
never filter, never invent ids.
"""

import html
import re

WS_RE = re.compile(r"\s+")
# Regulator-declared joint actions, e.g.
# "Joint recall with Health Canada, the United States Consumer Product
#  Safety Commission (US CPSC) and Taleco Gear."
JOINT_US_RE = re.compile(r"joint recall with[^.]*US CPSC", re.IGNORECASE)
WARNING_TITLE_RE = re.compile(r"health canada warns", re.IGNORECASE)


def _clean(text):
    if not text:
        return ""
    text = html.unescape(text)
    text = text.replace("\u200b", "")  # zero-width spaces seen in titles
    return WS_RE.sub(" ", text).strip()


def to_canonical(rec: dict, first_seen: str) -> dict:
    nid = str(rec.get("NID", "")).strip()
    title = _clean(rec.get("Title", ""))
    action = _clean(rec.get("What you should do", ""))
    issue = _clean(rec.get("Issue", ""))

    if WARNING_TITLE_RE.search(title):
        record_type = "safety_warning"
    elif rec.get("Organization") == "Communications and Public Affairs Branch" \
            and "advisory" in title.lower():
        record_type = "advisory"
    else:
        record_type = "recall"

    joint_with = ["US"] if JOINT_US_RE.search(action) else []

    return {
        "id": f"CA:{nid}",
        "jurisdiction": "CA",
        "source": "canada-rsa",
        "record_type": record_type,
        "authority": _clean(rec.get("Organization", "")),
        "title": title,
        "product_name": _clean(rec.get("Product", "")),
        "brands": [],  # bulk file has no separate brand field; lives in title/product
        "hazard_text": issue,
        "hazards": [h.strip() for h in issue.split(" - ") if h.strip()],
        "risk_class_source": _clean(rec.get("Recall class", "")),
        "action_text": action,
        "category_source": _clean(rec.get("Category", "")),
        "date_published": "",  # bulk carries Last updated only (see SCHEMA.md)
        "date_updated": _clean(rec.get("Last updated", "")),
        "first_seen": first_seen,
        "url": _clean(rec.get("URL", "")),
        "lang": "EN",
        "joint_with": joint_with,
        "source_payload": rec,
    }

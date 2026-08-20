"""
Build layer — the baby/child view over every canonical store.

The canonical stores (data/canonical/*.jsonl) hold EVERYTHING each agency
publishes; per our architecture, baby relevance is never decided at ingest.
This builder is where that decision lives: it re-derives data/views/baby.jsonl
from scratch on every run — the view is disposable output, the canonicals are
the asset.

Inclusion is LENIENT by design (a build view can afford false positives;
silent false negatives are the sin), and every included record carries
`match_rule` so any inclusion can be audited:

  us_curated     US records — tinysafe-data is already baby-curated upstream,
                 so the whole US canonical enters the view by definition.
  category       the source's own category names a child domain
                 (EU "Toys" / "Childcare articles...", KR 어린이/유아 categories)
  title_kw       child keyword in title/product/model text
  hazard_kw      child keyword ONLY in the hazard text — the CPSC lesson:
                 products named like adult goods whose hazard says
                 "young children can..." (dressers, blinds, magnets)

Per-language keyword packs: EN, KR (한국어), and a small FR pack for the
Canadian FR twins. Extend packs here, never in adapters.
"""

import json
import re
from collections import Counter
from pathlib import Path

DATA = Path(__file__).parent / "data"
OUT = DATA / "views" / "baby.jsonl"

EN_KW = re.compile(
    r"\b(baby|babies|infant|infants|toddler|toddlers|child|children|childs|"
    r"child's|kid|kids|nursery|crib|cribs|cot|cots|bassinet|stroller|"
    r"strollers|pram|prams|pushchair|walker|walkers|high\s?chair|car\s?seat|"
    r"booster\s?seat|pacifier|soother|teether|teething|playpen|play\s?yard|"
    r"sleeper|sleepwear|pajama|pyjama|onesie|romper|bib|rattle|plush|"
    r"toy|toys|juvenile|youth)\b", re.I)
KR_KW = re.compile(
    r"유아|아동|어린이|아기|영유아|신생아|완구|장난감|젖병|젖꼭지|치발기|"
    r"유모차|카시트|보행기|아기띠|기저귀|물놀이|킥보드|어린이집")
FR_KW = re.compile(
    r"\b(bébé|bebe|enfant|enfants|jouet|jouets|poussette|berceau|biberon|"
    r"sucette|siège\s?d'auto)\b", re.I)
EU_BABY_CATS = {"toys", "childcare articles and children's equipment",
                "children's fashion accessories"}
KR_CAT = re.compile(r"어린이|유아|아동|완구")
HZ_CHILD_ACT = re.compile(
    r"child-?resistant|"
    r"(?:young\s+)?child(?:ren)?(?:'s)?\s+(?:can|may|could|might|are|is|who)\b|"
    r"\ba\s+child\s+(?:can|may|could|might)|"
    r"\bby\s+(?:young\s+)?children\b|"
    r"\b(?:infant|toddler|baby|babies)s?\b|"
    r"\bswallow(?:ed)?\s+by\b|\bchoking\b|\bcrib|bassinet|stroller\b",
    re.I)


def match(rec):
    j = rec.get("jurisdiction", "")
    if j == "US":
        return "us_curated"
    cat = (rec.get("category_source") or "").strip()
    if j == "EU" and cat.lower() in EU_BABY_CATS:
        return "category"
    if j == "KR" and KR_CAT.search(cat):
        return "category"
    title = " ".join(str(rec.get(k) or "") for k in
                     ("title", "product_name", "model", "brands", "category_source"))
    if EN_KW.search(title) or KR_KW.search(title) or FR_KW.search(title):
        return "title_kw"
    hz = " ".join(str(rec.get(k) or "") for k in ("hazard_text", "hazards"))
    # Child-INTERACTION phrasing only. A bare "children" in the hazard text
    # is not enough: EU chemical boilerplate ("...may harm the health of
    # children") hangs on adult shampoos and deodorants, and a lenient match
    # there floods the view with grown-up cosmetics. What we want is the
    # CPSC false-negative shape — an adult-looking product that a child
    # ACTS ON: "young children can slip through", "a child may swallow",
    # "lacks child-resistant packaging".
    if HZ_CHILD_ACT.search(hz) or KR_KW.search(hz):
        return "hazard_kw"
    return None


def view_row(rec, rule):
    return {
        "id": rec.get("id"),
        "jurisdiction": rec.get("jurisdiction"),
        "record_type": rec.get("record_type"),
        "date": rec.get("date_published") or rec.get("date_updated") or "",
        "title": (rec.get("title") or rec.get("product_name") or "")[:200],
        "category": (rec.get("category_source") or "")[:100],
        "hazard": (str(rec.get("hazard_text") or "")[:200]),
        "url": rec.get("source_url") or rec.get("url") or "",
        "match_rule": rule,
    }


def main():
    OUT.parent.mkdir(parents=True, exist_ok=True)
    rows, stats = [], Counter()
    seen = set()
    for path in sorted((DATA / "canonical").glob("*.jsonl")):
        n_in = n_out = 0
        with path.open() as f:
            for line in f:
                if not line.strip():
                    continue
                rec = json.loads(line)
                n_in += 1
                rule = match(rec)
                if rule and rec.get("id") not in seen:
                    seen.add(rec.get("id"))
                    rows.append(view_row(rec, rule))
                    stats[f"{rec.get('jurisdiction')}:{rule}"] += 1
                    n_out += 1
        print(f"[*] {path.name}: {n_in} canonical -> {n_out} in baby view")
    rows.sort(key=lambda r: r["date"], reverse=True)
    tmp = OUT.with_suffix(".tmp")
    with tmp.open("w") as f:
        for r in rows:
            f.write(json.dumps(r, ensure_ascii=False) + "\n")
    tmp.replace(OUT)
    print(f"[*] baby view: {len(rows)} records -> {OUT}")
    for k, v in sorted(stats.items()):
        print(f"    {k:24} {v}")


if __name__ == "__main__":
    main()

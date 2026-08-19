# ARCHITECTURE — Global Recall Data Layer

The goal, stated precisely: **no data is ever lost, and every derived value
can be reproduced from stored originals.** Perfection is not "never making a
parsing mistake" — it is "every mistake is recoverable."

## The four layers

```
[L0 raw]        exactly what the source sent, dated, immutable
     |
[L1 adapters]   one translator per source -> canonical schema
     |
[L2 canonical]  one schema for every recall on earth: data/canonical/<src>.jsonl
     |
[L3 links]      cross-country joins + product-type clusters: data/links/
     |
[builds]        baby views, insight cards, B2B exports, app feeds
```

Everything under data/ is either **raw** or a **build output**. Nothing is
hand-edited. (Lesson: recalls_unified.json in tinysafe-data is a build
output nobody edits — same rule here.)

### L0 — raw is sacred
Every fetch is stored as received, under `data/raw/<source>/`. Never modified,
never "cleaned in place". Lesson learned the hard way: the `heading` field
was silently dropped from an app payload once, and three separate bugs traced
back to it. If you derive and discard the original, no rule can verify its
own work.

Exception for full-snapshot bulk sources (e.g. Canada, whose daily file IS the
complete history): keeping every daily 20MB copy would bloat the repo for no
information gain, so we keep `latest.json.gz` (overwritten) — the canonical
layer preserves everything that ever appeared, with `first_seen`.

### L1 — adapters are independent
One file per source in `adapters/`. A broken Canada adapter must never stop
the EU flow. Adapter contract:
- map what you can to the canonical schema
- everything you cannot map goes UNCUT into `source_payload`
- never filter. No baby filter, no severity filter. (CPSC lesson: keyword
  filtering at ingest silently dropped tip-over dressers, blind cords,
  safety gates.) Curation is a build step, not an ingest step.

### L2 — canonical schema (see SCHEMA.md)
Non-negotiables baked in:
- **ids are never invented**: `{jurisdiction}:{origin agency id}` —
  `US:21-198`, `CA:82446`, `EU:A12/01234/26`
- **record_type**: `recall` / `safety_warning` (firm refuses or is gone —
  no remedy) / `category_alert` (UK-style: a whole product type)
- source's own hazard text AND our derived hazard categories side by side;
  derived is multi-value (never reduce good prose to one keyword)
- source's own risk grade preserved verbatim (EU risk level, CA Class/Type,
  UK serious/high) — our tier lives in a separate field
- original language kept alongside English
- `date_published` (source's date) vs `first_seen` (our collection date)

### L3 — links, not merges
Cross-country records of the same product are LINKED with evidence, never
merged. (Lesson: folding Rock 'n Play 19-105 into 23-088 would have erased
"70 more deaths after the first recall".) Link evidence types, strongest
first:
1. **declared** — the regulator says so ("Joint recall with Health Canada
   and the US CPSC" appears verbatim in Canadian action text)
2. **registry** — OECD guid identity (US 26688 == CA 82446)
3. **barcode** — GTIN match (EU and CA publish barcodes)
4. **fuzzy** — product name + firm + 7-day window (weakest; the missing
   term that broke earlier dedup attempts was the firm name)

Product-type clusters (e.g. "silicone pull-string infant toys, 6 recalls,
2 countries, 3 weeks") also live here.

## Operating rules
1. Print what a change will touch, read it, write the rule, print what
   entered and left, compare against expectation.
2. Count from the written file, never from a run log.
3. Adding a source costs one collector + one adapter, nothing else.

# SCHEMA — canonical record (L2)

One JSON object per line in `data/canonical/<source>.jsonl`.

| field | type | rule |
|---|---|---|
| `id` | str | `{jurisdiction}:{origin id}` — never invented, never rewritten. `CA:82446` |
| `jurisdiction` | str | ISO-ish: US, CA, EU, GB, KR, AU, TW ... |
| `source` | str | which pipeline produced it: `canada-rsa`, `oecd-portal`, `eu-safety-gate` |
| `record_type` | str | `recall` \| `safety_warning` \| `category_alert` \| `advisory` |
| `authority` | str | origin agency as the source names it (CFIA, "Consumer product safety", TC) |
| `title` | str | source title, entities cleaned |
| `product_name` | str | |
| `brands` | [str] | array, never a joined string (multi-make lesson from NHTSA) |
| `hazard_text` | str | the source's own words, verbatim |
| `hazards` | [str] | derived categories, MULTI-value |
| `risk_class_source` | str | source's own grade verbatim: `Class 1`, `Type II`, `serious` |
| `action_text` | str | what the consumer should do, source's words |
| `category_source` | str | source's own category label |
| `date_published` | str | ISO date from the source (empty if source omits it) |
| `date_updated` | str | source's last-updated when provided |
| `first_seen` | str | when OUR pipeline first stored it |
| `url` | str | real per-record URL at the origin |
| `lang` | str | language of the text fields |
| `joint_with` | [str] | jurisdictions the SOURCE ITSELF declares a joint action with |
| `source_payload` | obj | the original record, uncut |

Rules:
- No field is ever deleted to save space; `source_payload` is the safety net.
- Derived fields (`hazards`, `record_type` when inferred) must be
  reproducible by re-running the adapter over `source_payload`.
- Baby relevance is NOT a field here — it is a build-time view, so the
  filter can improve without touching stored data.

## Known per-source quirks

**canada-rsa** (bulk `HCRSAMOpenData.json`)
- fields: NID, Title, URL, Organization, Product, Issue,
  "What you should do", Category, "Recall class", "Last updated", Archived
- `Issue` is quasi-structured, multi-value on " - "
  ("Fall hazard - Strangulation hazard")
- `Recall class` filled for food (Class 1–3) and health (Type I–III),
  empty for consumer products
- bulk file carries only `Last updated`, not the original publication date —
  documented gap; detail pages have it if ever needed
- ★ action text explicitly declares joint recalls:
  "Joint recall with Health Canada, the United States Consumer Product
  Safety Commission (US CPSC) ..." → `joint_with: ["US"]` — regulator-
  declared L3 links, no fuzzy matching needed
- "Health Canada warns that ... may pose ..." titles are warning-pattern
  records (product usually from Amazon/marketplaces, remedy = dispose)
  → `record_type: safety_warning`
- NID equals the OECD portal's CA native id (82446 == EN/CA/82446) —
  registry-level join key confirmed

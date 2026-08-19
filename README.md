# oecd-global-recalls

Daily collector for the OECD GlobalRecalls portal (https://globalrecalls.oecd.org).

**Standalone repo, deliberately independent of `tinysafe-data`.** This is a
signal-gathering experiment (global recall pattern detection). Nothing here
feeds the TinySafe app. If the experiment proves out, a curated slice can be
promoted into the main pipeline later.

## Why daily collection is not optional

The portal's only machine-readable surface is a **7-day rolling RSS feed**
(`/ws/rss.xqy`). There is no public bulk/history API — the documented API is
for government uploads only, and the portal itself is a JS SPA. A day not
collected within the window is data lost. Historical backfill, if ever needed,
goes to each country's ORIGIN source (Safety Gate, Health Canada, ACCC, KCA),
not to OECD.

## Layout

```
collect_oecd.py            # stdlib-only; fetch + parse + merge
data/raw/YYYY-MM-DD.xml    # untouched daily snapshots (audit trail)
data/recalls_global.jsonl  # one record per recall, deduped, sorted by date
.github/workflows/collect.yml  # twice-daily cron
```

## Record shape

```json
{
  "guid": "http://PoliciesApplications.oecd.org/GlobalRecalls/Recall/EN/AU/PS1134908",
  "lang": "EN",
  "country": "AU",
  "country_name": "Australia",
  "native_id": "PS1134908",
  "title": "Pull String Interactive Toys",
  "description": "Pull string silicone sensory toy for infants ...",
  "date_submitted": "2026-08-18",
  "portal_link": "https://globalrecalls.oecd.org/#/recalls/...",
  "first_seen": "2026-08-19"
}
```

## Decisions baked in

1. **No filtering at collection.** No baby filter, no country filter, no date
   filter. The CPSC work showed keyword filtering at ingest silently drops
   real child hazards (tip-over dressers, blind cords, safety gates). Collect
   everything; curate downstream.
2. **Dedup key = `(country, native_id)`**, language-independent. Canada
   publishes every recall twice (EN + FR); this folds them to one record,
   preferring EN.
3. **`date_submitted` vs `first_seen`.** The US is actively backfilling
   year-2000 records into the current feed, so "appeared in feed" does not
   mean "recent". Downstream analysis must filter on `date_submitted`.
4. **US records are collected but are NOT the US source of truth.** OECD's US
   rows are thin CPSC re-publications (~5 fields vs 73 in tinysafe-data).
   Their value here is the `native_id` — a join key for cross-country
   pattern detection (e.g. US 26688 = CA 82446, same Taleco Gear jumper).
5. **Scope caveat: OECD GlobalRecalls is NON-FOOD consumer products only.**
   Global food/formula recalls need each country's food agency and are out of
   scope for this repo.

## Known origin-source quirks

- Korea / Bulgaria native ids contain slashes (`K26/EP/10022792`,
  `SR/03722/25`) — never split native_id on `/`.
- Canada FR records sometimes have no description at all.
- Some feed entries omit `description` entirely (typical for CA).

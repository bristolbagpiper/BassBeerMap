# Location audit and confidence

A blue pin has accepted evidence. It does not guarantee the exact building or that Bass is currently on sale.

## Full independent report

- [Review order and summary](audit/full-location-audit.md)
- [CSV](audit/full-location-audit.csv)
- [All source comparisons, coordinates, dates and confidence](audit/full-location-audit.json)

The independent audit queries the official FSA API for all businesses at each listed full postcode, and also the reviewed reference postcode when there is a known discrepancy. Name matching is conservative; paginated results, ambiguous coordinates, missing matches and outages remain inconclusive. A FSA geocode can be a postcode centre.

Distances of up to 25 metres are reported as coordinate agreement, 25-50 metres as nearby, and over 50 metres as a disagreement requiring review. These thresholds are triage, not building-level proof. Rechecking FSA evidence against FSA is not labelled independent corroboration. Owner-reviewed pins retain their separate confidence classification even if sources disagree.

The report sorts known postcode discrepancies first, then differences over 50 metres, nearby or inconclusive evidence, then close agreement. Every current location appears in the report. Results for an older coordinate do not count as a check of a newly approved point.

## Recurring jobs

- **Daily map validation**: 45 rotating source checks at 06:47 UTC; checks committed assets and the live release, refreshes confirmed evidence and quarantines source conflicts under the existing policy.
- **Independent location audit**: 45 rotating FSA comparisons at 04:17 UTC. With 1,044 locations this covers the directory in about 24 daily runs. Oldest checks take precedence, so persistent conflicts do not starve the rotation.
- A complete independent audit can also be started with the workflow's `full` option.
- Changed actionable disagreements, source outages and failed jobs use the existing review issue and SMTP notification pipeline. Unchanged issues stay quiet. Nearby and missing-name results remain visible in the full report for manual review.

The independent audit never moves or recolours a pin. It records findings and opens review flags; corrections require reviewed evidence. Pin signatures invalidate stale comparisons after a correction. Historical owner decisions and previous coordinates remain in the registry.

## Run locally

`python -u full_location_audit.py --resume`

This writes a resumable checkpoint and full report under `candidate-independent/`, without changing the map. `--limit 45` rotates a smaller batch. `--publish --resume` updates reports and review flags after validating that every displayed location is unchanged. The reports are based on factual source comparisons, not an assertion that every building has been visually inspected.

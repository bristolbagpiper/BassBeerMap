# Location audit and confidence

A blue pin has accepted evidence. It does not guarantee the exact building or that Bass is currently on sale.

## What the job asks you to check

- **Action required:** a genuinely unresolved building/location, a changed business identity, a conflicting address that needs resolution, or a trading-status question. Each item states which fact needs checking; an identity or closure warning is not labelled an incorrect coordinate.
- **Source warnings:** failed or incomplete source checks. The last accepted pin is retained; retry the source rather than visiting or rechecking every pub.
- **Information — no pin check requested:** FSA coordinate differences, nearby matches, missing FSA names and other inconclusive comparisons. Passed controls and resolved history are not a manual task list.

Manual building confirmations are saved against the exact venue identity and
coordinate. They are not postal-delivery or Bass-stock approvals. A recorded
source-name variant is accepted only while its reviewed identity/address facts
remain unchanged; a different business, address or point reopens review.
Manual evidence still receives a freshness reminder after 180 days.

## Full independent report

- [Review order and summary](audit/full-location-audit.md)
- [CSV](audit/full-location-audit.csv)
- [All source comparisons, coordinates, dates and confidence](audit/full-location-audit.json)

The independent audit queries the official FSA API for all businesses at each listed full postcode, and also the reviewed reference postcode when there is a known discrepancy. Name matching is conservative; paginated results, ambiguous coordinates, missing matches and outages remain inconclusive. A FSA geocode can be a postcode centre.

Distances of up to 25 metres are reported as coordinate agreement, 25-50 metres as nearby, and over 50 metres as a coordinate disagreement. Coordinate differences alone are informational observations, not actionable location errors. These thresholds describe the comparison, not building-level proof. Owner-approved building coordinates take priority; CAMRA remains the primary automated reference. FSA supports address and identity checks and does not overrule either source based on coordinates alone. Rechecking FSA evidence against FSA is not labelled independent corroboration. Owner-reviewed pins retain their separate confidence classification even if sources disagree.

The report sorts known postcode discrepancies first, then informational differences or inconclusive evidence, then close agreement. Informational findings are explicitly labelled and excluded from the actionable review queue. Every current location appears in the report. Results for an older coordinate do not count as a check of a newly approved point.

## Recurring jobs

- **Daily map validation**: 45 rotating source checks at 06:47 UTC; checks committed assets and the live release, refreshes confirmed evidence and quarantines source conflicts under the existing policy. The same job then rotates up to 45 Google Places comparisons after publishing safe CAMRA results, within a combined 40-minute job deadline.
- **Independent location audit**: 45 rotating FSA comparisons at 04:17 UTC. With 1,044 locations this covers the directory in about 24 daily runs. Oldest checks take precedence, so persistent conflicts do not starve the rotation.
- A complete independent audit can also be started with the workflow's `full` option.
- Changed actionable postcode/source conflicts, source outages and failed jobs use the existing review issue and SMTP notification pipeline. Unchanged issues stay quiet. Nearby and missing-name results remain visible in the full report for manual review.

The daily job fails on a live release mismatch, invalid schema or failed fetch,
and records the result in `map-health-status.json` even when no safe candidate
can be published. Notifications combine the latest saved health result with the
location register. Repeated incidents stay quiet; a recovery produces one
changed-state email. Valid source checks can still publish during a live-site
failure. The review issue is owned by the notification workflow alone.

Source checks use current accepted evidence. Superseded and historical readings
remain in the registry but cannot validate a pin, supply a current address or
override an owner decision. Where several accepted CAMRA readings exist, the
newest reviewed reading supplies the expected identity and postcode. A source
refresh archives replaced readings instead of deleting their history. A genuinely
changed name, postcode or point still requires review.
Provider diagnostics attach confirmation only to the URL actually checked;
retained Google, FSA or other references are labelled `not_checked`.

Google live checks are **not active until configured**: set the repository
variable `GOOGLE_PLACES_ENABLED` to `true` and supply the Actions secret
`GOOGLE_PLACES_API_KEY` for an appropriately enabled Google Places API project.
Google billing, quotas and key restrictions must be configured for that project;
there is no guarantee of free usage. Missing or disabled configuration records
an explicit limitation in `google-places-status.json` and alerts through the
existing review pipeline, while CAMRA and FSA work can continue.

Google comparisons never automatically approve, quarantine, recolour or move a
pin. Conflicts and unavailable comparisons need review. The separate
`google-places-state.json` rotation and `google-places-status.json` snapshot
persist even when the Google API fails partway through a batch. Their public
content contains derived review flags and the directory's own listing fields,
not copied Google names, addresses, coordinates or raw API responses. The same
daily job uploads these files with its reports for seven days.

Google Places may reuse information from CAMRA or other sources; a Google
comparison does not establish independent provenance.

The adapter makes one bounded Text Search request per selected pub, without
pagination or automatic retries. Saved usage caps this job at 45 requests per
run/day and 1,400 per month; project quotas are still required to cover crashes
or other consumers of the same key. Checks expire after 30 days, and changed
listing identities or displayed pins immediately invalidate earlier checks.
The 50-metre marker tolerance is corroboration, not proof of the exact building
or current Bass availability. Missing configuration emits a workflow warning
and a persistent review limitation, rather than reporting a Google pass.

Notifications read main's current saved Google and map-health snapshots.
Changed Google findings or configuration/provider problems create an alert;
identical findings remain quiet despite new run IDs or dates, and recovery
produces one changed-state alert. A Google failure does not suppress safe
CAMRA publishing or replace the separate map-health result.

Each notification rechecks the Google snapshot against the current directory's
identity and pin signatures. Removed or changed pubs cannot retain an old
Google finding or an old all-clear result. Changed directory signatures and
missing or older-than-30-day status dates produce an incomplete-coverage
warning until checked again; a current-signature unresolved finding remains
visible for review even if its saved comparison is old.

The independent audit never moves or recolours a pin. It records all observations; coordinate-only differences never open review flags or trigger changed-location emails; corrections require reviewed evidence. Pin signatures invalidate stale comparisons after a correction. Historical owner decisions and previous coordinates remain in the registry.

## Run locally

`python -u full_location_audit.py --resume`

This writes a resumable checkpoint and full report under `candidate-independent/`, without changing the map. `--limit 45` rotates a smaller batch. `--publish --resume` updates reports and review flags after validating that every displayed location is unchanged. The reports are based on factual source comparisons, not an assertion that every building has been visually inspected.

`python -u full_location_audit.py --reports-only --publish` rebuilds classifications and reports from saved comparisons, without querying sources or updating their check dates. Existing review history is retained when coordinate-only flags are resolved.

The owner-decision notification step refreshes `audit/full-location-audit.*`
from saved comparisons automatically. Its actionable count describes that
independent comparison; the complete current location queue also covers
quarantined pins and other source failures.

An owner can resolve location review of a postcode discrepancy by confirming the pub building. The source postcode and reference postcode remain recorded; this does not assert that the postal delivery postcode was independently proven. The decision covers the exact pub name, postcode pair, reference URL and coordinate. Any change to these reopens review.

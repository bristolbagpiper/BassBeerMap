# Directory imports and pin verification

`venue-registry.json` owns permanent venue IDs and reviewed physical locations.
CSV names, places and postcodes remain editable metadata. Never create a new ID
for a venue merely because its name or postcode changes.

## Import gate

The monthly/upload workflow downloads and converts the source PDF, then runs
`ingest_directory.py` into `candidate-release/`. It never changes production
data until candidate validation and candidate browser tests pass. CSV, venue
registry, coordinates, evidence ledger, metadata and PDF are committed together.
The browser consumes `directory-release.json` alone, so cached versions of
separate CSV/coordinate files cannot be combined into an incorrect pin.

The gate rejects conflicting duplicates, unresolved identity changes, invalid
coordinates, missing coverage, evidence/coordinate mismatches and loss or
movement over 50 metres of previously verified pins. Existing reviewed locations
are preserved rather than replaced with a new automatic geocode. Unknown venues
require one unambiguous same-name, full-postcode CAMRA venue with a checked
named-page address and coordinate to get a precise pin. Discovery uses the
postcode search area, independently of FSA. Incomplete CAMRA coverage, different
postcodes, ambiguous venues and fuzzy names remain review candidates. FSA is an
additional address/coordinate comparison: missing records, outages and coordinate
disagreements do not veto a clear CAMRA match. A postcode search centre never
counts as venue-coordinate evidence. Unresolved venues retain an explicitly
approximate postcode pin and a review queue entry, with CAMRA candidate discovery
performed in the same import rather than waiting for the weekly research job.
Source outages preserve existing pins; failures to locate a new postcode reject
the release. Evidence older than 400 days cannot render as precise.

## Review an identity or duplicate conflict

Use the uploaded reconciliation report, source PDF, and independent named venue
address/map evidence. Add an explicit decision to `ingestion-decisions.json`
with source URL, check date and reason. Identity aliases must point to a known
previous key and record the reference postcode. Do not infer relocation from a
directory postcode change. Duplicate decisions list the precise accepted and
rejected field values; changed conflicts require another review.

For a confirmed physical relocation, review and update the existing registry
entry's pin, independent reference coordinates and dated evidence together.
The normal gate deliberately blocks movements over 50 metres compared with the
previous release. Publish such a reviewed correction as a dedicated reviewed
data/code commit, run release validation and browser tests, and preserve the old
evidence in Git history. Remove `review_required` only after that human review.
Do not automatically restore a pin that external checks disputed.

## Daily validation

CAMRA and Google are the two main comparison sources; FSA supplies supporting
information only. Google disagreements flag a manual review and never move a
pin automatically. A CAMRA match is not a claim that Google has also checked it.

`Daily map validation` runs every day at 06:47 UTC and supports manual dispatch.
It also runs after each successful PDF import, against the current main release.
Its historical filename is `weekly-map-validation.yml`. All GitHub cron times
are UTC; London summer time is one hour later. Scheduled starts can be delayed.
It validates every committed release asset and checks the live release ID. It
then rotates through up to 45 external checks, prioritising older checks while
reserving capacity for unresolved venues. This checks named addresses and
coordinates, not just HTTP availability. It retains evidence on source outages
and never changes a check date merely because a page responded successfully.

Confirmed source disagreements quarantine precise pins to approximate locations;
they never guess a replacement coordinate. Manual map evidence gets a review
reminder after 180 days and expires after 400 days. Unsupported/changed sources,
unresolved pins, stale evidence, live deployment mismatches and postcode conflicts
appear in one GitHub review issue. Unchanged reports remain quiet; the issue is
updated on meaningful changes and closed when resolved. Reports and staged
assets are retained as workflow artifacts. Monthly change-report email remains
available when SMTP secrets are configured.

Release validity and live health are separate results. A stale, unreachable or
invalid live release fails the audit even when its candidate files are valid.
Safe source checks can still be committed. `map-health-status.json` records the
latest live/pipeline result separately from the release bundle; notifications
read this durable status from current `main`, alongside the venue review queue.
Repeated identical health problems stay quiet, while a changed problem or
recovery is emailed once after SMTP accepts it. The daily job does not write a
second competing version of the review issue.

The `hermes-browser` provider combines the Windows Hermes Google checks with
the existing GitHub location review. Hermes records browser evidence locally.
GitHub validates the derived report against its current directory and sends
the consolidated alert through `Location review notifications`.
Hermes compares the visible named business, full postcode and actual place marker
with the saved pin. It rotates the oldest exact-bound attempts, up to 45 per run.
A marker more than 50 metres away, conflicting identity or possible closure is
flagged separately for review. A failed browser or consent/CAPTCHA block stops
the batch and leaves untouched venues unchecked. It never bypasses challenges.

Private receipts contain venue IDs, binding signatures, dated categories and
source links. Google checks expire after 30 days. The local report separates
the selected, attempted, checked, ambiguous and unavailable counts for the
execution from current coverage and prior unresolved attempts. Findings require
review before any listing or pin changes. The public map only displays possible
closure warnings with explicit dated evidence for the same venue and displayed
point. Legacy snapshots lack these fields.

The alternative `places-api` provider remains available but is not enabled by
this route. It requires both `GOOGLE_PLACES_ENABLED=true` and
`GOOGLE_PLACES_API_KEY`, plus a Google Places project with billing. Its request
limits remain 45 per day and 1,400 per month. The Hermes browser route uses no
Google API key or API billing and depends on the local computer being available.

The installed native Hermes job `1353548e1bb7` runs daily at 07:40 Europe/London,
using a separate clean local browser and a script-only job (no model calls).
State and private receipts live under `%LOCALAPPDATA%\hermes\profiles\mcjeeves\bass-map-google`.
Install a tested Git revision with `python install_hermes_google.py --revision
<commit> --state-dir <state-directory> --launcher <existing-cron-script>`.
The installer stages versioned Python files from Git, writes a file-hash manifest
and switches the launcher after validation. The previous version stays available
for rollback through the same command.

Read `last-run.json` for the fetched data commit, installed code commit, batch
outcome and local report hash. The authenticated Codex handoff stages the
current GitHub release and registry at a single `main` commit, then prepares a
filtered report with `hermes_google_publish.py`. It checks the execution receipt
and rebuilds findings for the current identities and pins. The handoff updates
only `hermes-google-status.json`, with an expected parent commit. If the parent
moves, stage the new release and rebuild the candidate before retrying.

Publish a new execution only after checking its timestamp against the current
committed report. A repeated execution with an identical directory binding
needs only a review readback. Confirm that GitHub ran `Location review
notifications` for the new commit and consumed the expected Google execution.
The workflow also supports dispatch for a specific report ID. The publication
receipt records both the report commit and the review run. GitHub records SMTP
acceptance separately and suppresses unchanged consolidated findings.

Keep the local observation journal and source URLs outside Git. The handoff
publishes directory fields, derived review reasons and binding metadata.
Visitor-visible closure warnings require a separate dated finding for the
exact displayed venue and pin. Hermes review findings require corroboration
before any public listing or coordinate change.

## Location tracking and alerts

Every release includes `location-review.json`: permanent venue ID, name, place,
postcode, first-seen date, last-check date, FSA/CAMRA results, specific failure
reason, reference source and status history. Resolved and removed records remain
in the register. The candidate gate requires every approximate pin to appear in
the active register. Lookup failures never mean a source has no listing unless
that source was actually searched.

`Location review notifications` runs after imports, daily audits, research and
owner approvals, including
failed runs, and can be dispatched manually. It updates the GitHub review issue
and emails the configured `ALERT_TO` recipient using existing SMTP secrets.
The initial report includes all outstanding locations; later emails cover new
locations, changed failure reasons and resolutions. Identical outstanding reports
remain quiet. `location-alert-state.json` records successful SMTP acceptance only;
an exception or refused recipient leaves delivery unacknowledged for retry.
Notification failures fail visibly and open a separate GitHub issue. Successful
recovery closes that issue. SMTP acceptance does not prove inbox delivery.

Owner decisions also rebuild the independent report from saved comparisons,
including when an acknowledgment was already applied. This updates derived
classification only: source check dates, coordinates and approval facts are not
refreshed. Informational FSA differences do not become active health incidents.

## Fixing brown pins without AI

Reviewed `listing_corrections` in `ingestion-decisions.json` are scoped to an
exact venue identity and expected source fields. They can correct a postcode or
add a dated public note without changing another pub at the same postcode.
Postcode corrections retain the existing permanent ID and original-row history.
Exact `listing_exclusions` can remove a reviewed obsolete Bass listing while
retaining its registry and pin history. If that excluded listing changes in a
later PDF, publication stops for a fresh review rather than silently restoring
or discarding a new beer confirmation.

`Research unverified locations` runs Tuesdays at 08:17 UTC and supports manual
dispatch. It retries CAMRA-primary verification, records supporting FSA checks and searches
CAMRA around the listed postcode even when FSA matching fails. It rotates through
up to 30 unverified venues, retaining up to three possible named-address matches
per venue. Name variants and spelling similarities are suggestions only. Source
outages retain earlier candidates; incomplete searches are explicitly marked.
Research results and candidate changes appear in the existing review issue and
email alerts. No AI model or API key is used.

For an exception, the repository owner checks the candidate's named address and
map, then posts the report's `/verify-location <venue-id> <CAMRA-url>` command in
issue #5. Several commands can be submitted together, one per venue per line.
`Apply reviewed location` accepts only commands from the human repository owner.
It fetches fresh structured source evidence, rejects unrelated/distant/changed
candidates and verified relocations, stages the correction, validates all assets,
runs browser tests and publishes the reviewed location. It records the approval
comment, actor, source, date and previous pin against the same permanent venue ID.
Later source disagreements quarantine the location again and trigger an alert.

This is a human address/map review, not a colour-change button. A pub more than
6 km from the listed area or a relocation of an already precise pin requires a
dedicated review rather than bypassing the normal safeguards.

## Local checks

```
python -m unittest discover -s tests -v
python validate_directory_release.py
python validate_coordinate_verification.py --max-evidence-age-days 400
```

Set `RUN_BROWSER_TESTS=1` for headless Chrome tests. Set
`DIRECTORY_TEST_ROOT=candidate-release` to test staged assets with the current
HTML. `python ingest_directory.py candidate-pubs.csv --offline` skips external
venue enrichment; postcode lookups still run for previously unseen postcodes.

The GJ Muckers exception records the PDF's SK11 7NE versus CAMRA's Sunderland
Street SK11 6JL and preserves the independently checked location. Directions
for precise pins use coordinates, so conflicting directory postcodes cannot
send visitors elsewhere. The Black Lion exception retains the newer Q3 guest
listing rather than the duplicate Q2 permanent listing.

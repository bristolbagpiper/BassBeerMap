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
require an unambiguous full-postcode FSA match plus an independently mapped
same-name CAMRA venue agreeing within 50 metres to get a precise pin. Otherwise
they get an explicitly approximate postcode pin and a review queue entry.
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

## Weekly validation

`Weekly map validation` runs Mondays at 06:47 UTC and supports manual dispatch.
It validates every committed release asset and checks the live release ID. It
then rotates through up to 30 external checks, prioritising older checks while
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

# Directory imports and pin verification

`venue-registry.json` owns permanent venue IDs and reviewed physical locations.
The PDF owns the published listing fields, including names, places and postcodes.
Link a reviewed name or postcode change to the existing venue ID.

## Import gate

The weekly source-check/upload workflow downloads the source PDF before running
the import pipeline; identical scheduled PDFs skip conversion and tests, while
later source changes are picked up within a week. Uploads and deliberate
manual reindex requests still run the release gate. The workflow then runs
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
performed during the import. Additional research can be run manually.
Source outages preserve existing pins; failures to locate a new postcode reject
the release. Accepted physical-location evidence remains tied to its recorded
source, address and coordinate; age alone does not turn a known building into a
postcode-centre pin.

## Review an identity or duplicate conflict

Use the uploaded reconciliation report, source PDF, and independent named venue
address/map evidence. Add an explicit decision to `ingestion-decisions.json`
with source URL, check date and reason. Identity aliases must point to a known
previous key and record the reference postcode. Do not infer relocation from a
directory postcode change. An exact duplicate identity blocks publication until
all PDF records can be represented without selecting a survivor.

For a confirmed physical relocation, review and update the existing registry
entry's pin, independent reference coordinates and dated evidence together.
The normal gate deliberately blocks movements over 50 metres compared with the
previous release. Publish such a reviewed correction as a dedicated reviewed
data/code commit, run release validation and browser tests, and preserve the old
evidence in Git history. Remove `review_required` only after that human review.
Do not automatically restore a pin that external checks disputed.

## Scheduled integrity and manual research

CAMRA and Google are the two main comparison sources; FSA supplies supporting
information only. Google disagreements flag a manual review and never move a
pin automatically. A CAMRA match is not a claim that Google has also checked it.

`Map validation` runs daily at 06:47 UTC and supports manual dispatch. Its
historical filename is `weekly-map-validation.yml`. All GitHub cron times are
UTC; London summer time is one hour later. Scheduled starts can be delayed. It
validates committed release assets and reads the visitor-facing release bundle
back from the live map. It has read-only repository permission: it does not
recheck sources, create candidate data, write map/alert state, publish, or send
venue notifications. A live mismatch fails the integrity run visibly.

The FSA audit, CAMRA research, and Google comparisons remain manually usable.
Research creates a validated candidate artifact only. It does not approve its
proposed pins or publish them; physical-location corrections use the existing
owner-reviewed correction workflow. Changed directory editions publish
through the import workflow. Routine integrity checks do not move or refresh
accepted physical evidence.

Release integrity and live serving are checked separately. An unreachable or
invalid live release fails the integrity run, but it does not re-publish
coordinate data or trigger a venue email. A genuinely rejected import creates
its own durable `BassBeerMap update rejected` issue while retaining the
previously published release.

The `hermes-browser` provider combines the Windows Hermes Google checks with
the existing GitHub location review. Hermes records browser evidence locally.
GitHub validates the derived report against its current directory and sends
the consolidated alert through `Location review notifications`.
Hermes compares the visible named business, full postcode and actual place marker
with the saved pin. It rotates the oldest exact-bound attempts, up to 45 per run.
Up to five slots go to outstanding closure flags due for a follow-up after
three days. A newer conclusive comparison without a closure flag returns the
venue to the ordinary rotation.
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
The standard Hermes `default` profile owns the job. State, observation history,
private receipts and publication credentials live under
`%LOCALAPPDATA%\hermes\bass-map-google`. McJeeves is reserved for piping and
pipe-band responsibilities.
Use the scheduled job's Python to run `install_hermes_google.py --revision
<commit> --state-dir <state-directory> --launcher <existing-cron-script>
--scheduler-jobs <Hermes-home>/cron/jobs.json`. The installer checks the live
job, stages and hashes committed files, then switches the launcher under the
native job lock. Add `--check` and use the full installed commit to verify the
scheduler, launcher and code without changes. The production launcher accepts
no arguments.

Read `last-run.json` for the fetched data commit, installed code commit, batch
outcome and local report hash. After the browser finishes, the same native job
uses `hermes_google_publish.py` to rebuild the filtered report against the
current GitHub release. It publishes only `hermes-google-status.json`. The push
requires the exact expected parent. If `main` advances, the publisher checks
the new release and rebuilds the report before retrying.
The native execution succeeds only when the browser batch completes and the
publication attempt records the same execution, outcome and verified GitHub
commit. `review_pending` records a verified push while Actions confirmation
is still pending.

The publisher uses a BassBeerMap-only SSH deploy key. The private key and pinned
GitHub host configuration are stored under `publication-ssh` in the local
state directory with Windows permissions restricted to the scheduled account.
`publication-auth.json` refers to the SSH configuration and contains no key
material. `publication-repo.git` caches the public Git objects used for validation.

`publication-attempt.json` records the last attempt, including a safe failure
reason. `publication-receipt.json` records the verified push and any observed
GitHub review run. Run `hermes_google_runner.py --state-dir <state-directory>
--publish-only` outside the scheduler to retry a saved execution. Keep
`--local-only` diagnostics in a separate state directory. Repeated
evidence is published only when the bindings, findings or coverage change or
the GitHub report needs a freshness update. GitHub records SMTP acceptance
separately from inbox delivery. The scheduled map integrity check does not
depend on receipt of this local browser execution.

Keep the local observation journal and source URLs outside Git. The handoff
publishes directory fields, derived review reasons and binding metadata.
Visitor-visible closure warnings require a separate dated finding for the
exact displayed venue and pin. Hermes review findings require corroboration
before any public listing or coordinate change.

## Acceptance evidence after changes

1. Run the affected tests and the existing browser, handoff and location
   notification tests. Compare venue IDs and displayed coordinates before and
   after any release or frontend change.
2. Any change to the native Hermes schedule is a separate operator action on
   the exact fenced job; the GitHub integrity workflow does not own that job.
3. Open the live map, search for a venue and open its pin. For trading notice
   changes, also check active, due and removed notice behaviour. Store the
   commit and Actions run links.

Trading notices are maintained in `trading-notices.json`. Dated notices show
through `review_by` in UTC and then become a neutral prompt to check before
travelling. The same review workflow lists due notices in issue #5. Review
new Google findings and corroborating evidence, then edit or remove the notice
and check the deployed map. An inconclusive or missing Google result does not
establish reopening, and a property sale or closed kitchen does not establish
pub closure.

## Location tracking and alerts

Every release includes `location-review.json`: permanent venue ID, name, place,
postcode, first-seen date, last-check date, FSA/CAMRA results, specific failure
reason, reference source and status history. Resolved and removed records remain
in the register. The candidate gate requires every approximate pin to appear in
the active register. Lookup failures never mean a source has no listing unless
that source was actually searched.

`Location review notifications` runs after successful imports and owner
approvals, on relevant source pushes (including notice-only changes), and can be
dispatched manually. Rejected imports keep their separate `BassBeerMap update
rejected` issue and do not enter the location-email loop. The notification run
updates issue #5 with the current review queue and emails only newly changed
owner questions or newly due public notices. Source outages, map-health and
Google diagnostics, old history and cleared decisions do not create email
questions. A changed venue is sent by itself; unrelated venue changes do not
repeat already acknowledged questions. Clearing a decision prunes its email
acknowledgement so a future recurrence can be sent again.

`location-alert-state.json` records each question after successful SMTP server
acceptance only; an exception or refused recipient leaves it unacknowledged for
retry. A quiet run still records current cleared state but sends nothing.
Notification failures fail visibly and open a separate GitHub issue. Successful
delivery of a changed question closes that issue. SMTP acceptance does not
prove inbox delivery. The daily map check keeps its read-only integrity schedule, while
Google comparisons and the full independent FSA audit remain manually usable;
weekly research is manual-only.

Owner decisions also rebuild the independent report from saved comparisons,
including when an acknowledgment was already applied. This updates derived
classification only: source check dates, coordinates and approval facts are not
refreshed. Informational FSA differences do not become active health incidents.

## Fixing brown pins without AI

The PDF governs inclusion and all source listing fields. Import validation
compares the complete candidate against the PDF and checks the printed total
when present. If multiple source rows have the same identity and cannot yet be
represented by the release format, the import stops for review with all input
rows intact.

Reviewed `identity_aliases` link source spellings and postcodes to the same
permanent venue and pin. Alternative names and evidence-backed navigation
addresses are separate from the PDF fields. `postcode-overrides.json` supplies
lookup references for postcodes that the normal lookup cannot use. Previous
listing corrections, exclusions and duplicate decisions are kept as historical
evidence in `ingestion-decisions.json` and the registry. Trading notices use
`trading-notices.json`. Investigate discrepancies when they affect the identity
of the mapped building or the accuracy of its pin.

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
python validate_directory_release.py --check-pdf
python validate_coordinate_verification.py --max-evidence-age-days 400
```

Set `RUN_BROWSER_TESTS=1` for headless Chrome tests. Set
`DIRECTORY_TEST_ROOT=candidate-release` to test staged assets with the current
HTML. `python ingest_directory.py candidate-pubs.csv --offline` skips external
venue enrichment; postcode lookups still run for previously unseen postcodes.

The GJ Muckers exception records the PDF's SK11 7NE versus CAMRA's Sunderland
Street SK11 6JL and preserves the independently checked location. Its directions
open the reviewed coordinates. Where a separate navigation address is verified,
the map can use that address while continuing to display the PDF postcode.

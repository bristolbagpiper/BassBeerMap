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

The listing change report is attached to the workflow run. A manual import
can request an emailed copy. Scheduled imports leave the digest in the artifact.

The import workflow runs for a root-level PDF push, not ordinary code pushes.
It selects the single PDF changed by that exact push commit. A non-canonical
PDF is staged as an incoming source without replacing or deleting other PDFs;
multiple changed PDFs are rejected as ambiguous. A changed
`latest-bass-directory.pdf` is treated as an already-canonical release and
receives the same full PDF-to-CSV validation. The workflow bot does not retrigger
itself. Code and canonical PDF changes also pass through the read-only
`Validate coordinates` PR/main checks, including `--check-pdf`.

For unattended releases, `promote_checked_commit.py` places the single data
commit on a temporary branch and dispatches the existing validation workflow.
It verifies the GitHub run ID, commit and result and then fast-forwards
`main` from the expected parent. A failed check or advanced parent stops
promotion. The importer and owner approval workflow request a Pages build
and check its commit and the public HTML, PDF, CSV and bundle bytes.
The branch rule requires the GitHub Actions `validated-directory` commit
status and has no bypass actors. A separate job records the tested SHA and
links the run only after `coordinate-audit` succeeds. This lets the same
validation protect both manual and unattended releases.

The gate rejects conflicting duplicates, unresolved identity changes, invalid
coordinates, missing coverage, evidence/coordinate mismatches and loss or
movement over 50 metres of previously verified pins. Existing reviewed locations
are preserved rather than replaced with a new automatic geocode. Unknown venues
require one unambiguous same-name, full-postcode CAMRA venue with a checked
named-page address and coordinate to get a reference pin. Discovery uses the
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

### Optional Google research

The legacy Hermes browser comparison and its local evidence remain available
for targeted investigation. Run `hermes_google_runner.py --local-only` with
a separate state directory for diagnostics. Compare the named venue, full
address and marker with independent premises evidence before changing a pin or
writing a dated trading notice. Google search results and coverage statistics
do not publish visitor information or generate owner emails.

The previous daily 07:40 Europe/London job is `1353548e1bb7` under the
standard Hermes `default` profile. The cutover pauses that job using its
per-job scheduler lock. Its previous observations, receipts and credentials
are retained under `%LOCALAPPDATA%\hermes\bass-map-google`. The scheduled
directory import and live map check run on GitHub independently of this machine.

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

Issue #5 holds the current review queue and the notification receipt marker. The
committed `location-alert-state.json` is only a bootstrap seed for historical
SMTP receipts; ongoing acknowledgements do not write to protected `main`. Each
changed question is reserved on issue #5 before SMTP is attempted. Known
pre-send failures release the reservation when the issue update succeeds. A
timeout, refusal, or other failure during SMTP is treated as ambiguous and held
from automatic resend. Failure to record a later receipt also leaves the
pre-send reservation in place, preventing a duplicate.

The workflow reports SMTP acceptance, not inbox delivery or human use. For a
held item, check the actual mailbox outcome and manually dispatch the workflow
with `confirm_sent` or `confirm_not_sent`; the former records the receipt and
the latter releases the reservation for a new attempt. There is no automatic
retry claim for ambiguous outcomes. A quiet run still prunes cleared questions
from active receipt state, so a genuinely reappearing question can be sent
again. Notification failures fail visibly and open a separate GitHub issue.
The daily map check keeps its read-only integrity schedule, while
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

`Research unverified locations` runs on request. Supply a venue ID to investigate
one location, or leave it blank to check up to 30 postcode, reference and site
pins. The workflow saves candidates and source warnings as a review artifact.
Existing pins stay in place. A newly discovered named CAMRA point is a reference
location until the premises are identified.

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

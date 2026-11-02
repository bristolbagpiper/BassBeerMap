# Reviewing difficult venue pins

`venue-registry.json` owns stable venue IDs, accepted physical locations and
review history. The browser reads the generated `directory-release.json`.
Follow [Directory imports and pin verification](INGESTION.md) for the current
staged import and approval process; [Location audit and confidence](LOCATION_AUDIT.md)
explains what source agreement can establish.

## Current review path

Use `location-review.json` and issue #5 to identify a disputed or approximate
pin. Research proposals are candidates, not approved coordinates. Check the
named venue, address and map; the human repository owner can submit the
recorded `/verify-location` command for an eligible unverified location.
Approval stages a complete release, records provenance against the existing ID,
and runs validation and browser checks before publication.

A rename, locality label or postcode change must retain its reviewed identity.
When an incoming row could replace a prior venue, add a fact-scoped reviewed
alias in `ingestion-decisions.json`; do not mint a new identity to evade a
disputed pin. A relocation of an already precise pin requires a dedicated
review. Later source disagreements quarantine pins until reviewed again.

## Historical maintenance tools

`venue-coordinate-overrides.json` and `audit/manual-place-checks.csv` retain
reviewed inputs and compatibility history. They are not a substitute for the
current registry and complete release validation. Evidence source, date and
reference coordinate are required for an accepted pin.

The old OSM resolvers, `audit/apply_*` scripts, bare batch converter and
`build_coordinate_review_queue.py` are maintenance tools. Run historical
reproduction commands only in an isolated copy. They can write individual files
without the current stage gate, and editing an override alone does not update
the live browser bundle. Preserve their history; use the staged workflow for
normal operation.

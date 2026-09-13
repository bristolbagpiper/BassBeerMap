# Bass Beer Map coordinate audit

Completed 13 September 2026. This audit covers every one of the 1,102 unique
directory listings. It is an evidence audit, not a claim that any third-party
map database can never contain an error.

## Result

- 1,102 of 1,102 listings have a source-linked venue coordinate.
- 1,056 current points agree within 50 metres with a separately maintained
  CAMRA venue record.
- 46 remaining cases were individually checked against a named map place,
  full address, official venue site, FSA record, or a combination of those.
- 0 pins rely on a postcode centroid in the audited build.
- 39 listing postcodes conflict with the venue reference but no longer move
  the pin to the wrong postcode location.
- 3 obvious source-name defects remain recorded for correction.

The 42 metadata defects are deliberately retained in `anomaly-queue.csv`.
They do not make the audited pin positions approximate, but they should be
fixed in the upstream directory data rather than silently hidden.

## Production safety rule

`coordinate-verification.json` is a complete, generated ledger. `index.html`
only renders a blue venue pin when that ledger says `verified_venue`. If the
ledger is absent, stale, incomplete, or cannot be loaded, the map fails closed
to postcode pins instead of presenting unverified coordinates as exact.

CI regenerates the ledger, rejects any diff, requires all listings to be
verified, rejects missing evidence, checks manual-review coordinates against
their source CSV, catches reused coordinates across different postcodes, and
requires evidence to be no more than 400 days old.

## Files

- `pin-audit.csv` / `pin-audit.json`: full CAMRA comparison for every listing.
- `fsa-comparison.csv`: independent FSA checks for cases not settled by CAMRA.
- `manual-place-checks.csv`: the 46 individually reviewed cases, including
  exact coordinates, addresses, URLs, dates, and review notes.
- `anomaly-queue.csv`: the remaining 39 postcode and 3 source-name defects.
- `../coordinate-verification.json`: production evidence ledger consumed by
  the map.

## Re-run

```bash
python audit/apply_manual_place_checks.py
python audit/compare_pins.py
python audit/compare_fsa.py
python build_coordinate_verification.py
python validate_coordinate_verification.py --require-all-verified --max-evidence-age-days 400
python audit/build_action_queue.py
python -m unittest tests.test_coordinate_audit_regressions tests.test_coordinate_verification
```

Refreshing CAMRA/FSA source caches is a separate, networked collection step;
the checked cache snapshot is intentionally not required for normal CI.

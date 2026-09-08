# Reviewing difficult venue pins

`venue-coordinate-overrides.json` is the permanent, version-controlled place
for a manually verified venue coordinate. It takes precedence over the
automated OSM result, so it remains in effect after every monthly directory
refresh.

Only add a coordinate after checking that the venue itself is the result — not
merely the postcode or the town centre. Record where it was checked so a later
review is possible:

```json
{
  "venues": {
    "example inn|example town|ab1 2cd": {
      "lat": 51.5000,
      "lng": -0.1200,
      "source": "manual-openstreetmap-review",
      "source_url": "https://www.openstreetmap.org/node/123",
      "checked_at": "2026-09-08"
    }
  }
}
```

The map only requires `lat`, `lng`, and `source`; the provenance fields are
there for auditability. Never copy a postcode-centre coordinate into this file.

## Finding the next venue

Run:

```powershell
python build_coordinate_review_queue.py
```

It produces `coordinate-review-queue.csv`, containing only listings without an
automated exact venue match or a manual override. The monthly GitHub workflow
also creates the same file as an artifact and writes the count into its run
summary. That means new difficult listings appear in one place instead of
quietly falling back to an approximate pin.

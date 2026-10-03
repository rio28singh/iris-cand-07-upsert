# IRIS-CAND-07: Safe Staging-to-Core Upsert Promotion (Parcels)

Promotion logic that moves staged parcel records into a core table safely: corrected source rows replace older data instead of being silently skipped, bad rows are rejected with a reason you can inspect, and re-running the same input changes nothing.

- No `INSERT ... ON CONFLICT DO NOTHING` (corrections would be lost).
- No `DELETE ALL + INSERT ALL` (history and untouched rows would be lost).
- Deterministic and auditable: every run records counts, rejects, and a JSON manifest.

Stack: Python 3.12+, PostgreSQL 16+, PostGIS 3.4+, psycopg 3.

Verified on PostgreSQL 16 / PostGIS 3.4.2 (Linux, Python 3.12) and on Windows (Python 3.13, Docker): 33/33 tests pass.

## Table of contents

- [Quick start](#quick-start)
- [Commands](#commands)
- [What it does](#what-it-does)
- [Architecture](#architecture)
- [Data contracts](#data-contracts)
- [Promotion rules](#promotion-rules)
- [Idempotency and audit](#idempotency-and-audit)
- [Tests](#tests)
- [Task checklist](#task-checklist)
- [Assumptions and simplifications](#assumptions-and-simplifications)
- [Troubleshooting](#troubleshooting)
- [Project layout](#project-layout)

## Quick start

Requirements: Python 3.12+, Docker (for PostgreSQL 16 + PostGIS 3.4), Git.

Windows users: see `WINDOWS_SETUP.md` for a click-by-click guide.

```bash
# 1. Start the database (PostgreSQL 16 + PostGIS 3.4; creates databases `iris` and `iris_test`)
docker compose up -d

# 2. Create a virtual environment and install
python -m venv .venv
source .venv/bin/activate            # Windows (CMD): .venv\Scripts\activate
pip install -e ".[test]"

# 3. Prove it works
pytest                               # expect: 33 passed

# 4. Replay the full story (insert, rerun, correction, stale, conflict, authority)
iris-promote demo
```

The defaults match the compose file (`postgres:postgres@localhost:5432`), so no configuration is needed. To use a different database, set:

| Variable | Used for | Default |
| --- | --- | --- |
| `IRIS_DATABASE_URL` | the app and the demo | `postgresql://postgres:postgres@localhost:5432/iris` |
| `IRIS_TEST_DATABASE_URL` | the tests (schema is wiped per test) | `postgresql://postgres:postgres@localhost:5432/iris_test` |

Without Docker, any PostgreSQL 16+ with PostGIS 3.4+ works; the DB user must be allowed to run `CREATE EXTENSION postgis`.

The credentials above are local-development defaults only; no production secrets are used anywhere.

## Commands

```bash
iris-promote migrate                                        # apply SQL migrations (idempotent)
iris-promote demo                                           # reset the 'iris' schema, replay 6 runs, write manifests/
iris-promote run fixtures/batch1_initial.csv --batch-id b1 --manifest manifests/m.json   # load + promote
iris-promote load fixtures/x.csv --batch-id b5 --source-rank 10                          # stage only
iris-promote promote --batch-id b5 --manifest manifests/m.json                           # promote a staged batch
iris-promote inspect-rejects --run-id 1                     # list rejected rows with reasons
pytest                                                      # 33 tests
```

`--source-rank` is the authority of the source system for that batch (higher wins ties). Default `0`.

## What it does

```text
CSV --> load --> staging_parcels      loose text, append-only, never edited
                   |
                   |   iris.promote_parcels(batch_id)    one transaction, advisory-locked
                   v
  1. parse + validate each row  -----------------> rejected_parcels (reason + raw row as JSON)
  2. pick ONE winner per key inside the batch      (others: SUPERSEDED_IN_BATCH)
  3. compare winner with core_parcels              (content hash + freshness)
  4. INSERT ... ON CONFLICT DO UPDATE ... WHERE <different AND strictly fresher>
  5. write counts, change log, manifest
```

### Demo result (`iris-promote demo`)

| Run | Input | Result |
| --- | --- | --- |
| 1 | `batch1_initial.csv` (8 rows) | inserted 3, rejected 5 (bad rows, each with a reason) |
| 2 | same file again | inserted 0, updated 0, unchanged 3 (no duplicates) |
| 3 | `batch2_corrections.csv` | inserted 2, updated 1 (geometry and attributes), unchanged 2 |
| 4 | corrections again | everything unchanged |
| 5 | `batch3_stale_and_conflict.csv` | both rejected: `STALE_SOURCE`, `CONFLICT_SAME_FRESHNESS` |
| 6 | `batch4_authoritative.csv` (rank 10) | updated 1 (higher authority wins the tie) |

Final core: `DE/P-100`, `DE/P-200`, `DE/P-300`, `FR/P-100`, `NL/P-100`.

Note: `P-100` exists in three countries at once without colliding.

## Architecture

| Decision | Why |
| --- | --- |
| Staging holds raw text | Bad source rows can be loaded, then rejected with a reason, instead of crashing the load. |
| Promotion is one SQL function (`iris.promote_parcels`) | Set-based, atomic, testable in the database itself; no row-by-row Python loops. |
| Whole run in one transaction | A failure leaves no partial data and no half-written run record. |
| `pg_advisory_xact_lock` | One promotion at a time; removes read-then-write races. |
| `ON CONFLICT DO UPDATE ... WHERE` guard | Second line of defence: an older or identical row can never overwrite a newer one, even under a race. |
| Counts derived from what actually happened | The change log drives inserted/updated; a table CHECK enforces staged = inserted + updated + unchanged + rejected. |
| Constraints in the database | Primary key, foreign key, NOT NULL and geometry validity hold even if someone bypasses the Python code. |

## Data contracts

### Natural key

- `PRIMARY KEY (country_code, parcel_ref)`. Country is part of the key, is `NOT NULL`, and has a foreign key to `iris.countries`.
- The same `parcel_ref` in two countries can never collide.

### Geometry

- Column `geom`, `geometry(MultiPolygon, 4326)`, `CHECK ST_IsValid`.
- Polygons are wrapped with `ST_Multi` (lossless).
- Invalid geometry is rejected, never repaired.

### CRS

- `srid` must be declared and equal `4326`.
- Missing or different means rejected.
- No silent reprojection or guessing.

### Units

- `area_m2` is square metres, `>= 0`.
- If blank it stays `NULL`; it is never computed or invented.

### Source date

- Required, parseable, not in the future.

### Freshness

- `source_date`, `source_rank` compared lexicographically.

### Completeness

- Required: `country`, `parcel_ref`, `source_date`, `CRS`, valid geometry.
- Optional: `land_use`, `area_m2` stay `NULL` if absent.

### Uncertainty

- This slice carries no accuracy or uncertainty values and never fabricates any.
- Production would add an explicit `geom_accuracy_m` column and source confidence.
- The project's "indicative material" wording applies to downstream outputs, which are out of scope here.

## Promotion rules

Per incoming key, after in-batch de-duplication (newest `source_date`, then highest `source_rank`, then latest `staging_id` wins):

| Situation | Outcome |
| --- | --- |
| No core row | `INSERTED` |
| Same content hash | `UNCHANGED`. If strictly fresher, only the freshness stamp is refreshed (no version bump, no updated_at change). |
| Different content, strictly fresher | `UPDATED` (version + 1, change log lists the changed columns) |
| Different content, older | rejected `STALE_SOURCE` |
| Different content, same freshness | rejected `CONFLICT_SAME_FRESHNESS` (a human decides) |
| Several staged rows, same key | newest wins; the rest are `SUPERSEDED_IN_BATCH` |
| Invalid row | rejected: `UNKNOWN_COUNTRY`, `MISSING_PARCEL_REF`, `INVALID_SOURCE_DATE`, `FUTURE_SOURCE_DATE`, `MISSING_CRS`, `UNSUPPORTED_CRS`, `MISSING_GEOMETRY`, `INVALID_WKT`, `UNSUPPORTED_GEOMETRY_TYPE`, `INVALID_GEOMETRY`, `INVALID_AREA` |

Change detection: `sha256(land_use, area_m2, ST_AsEWKB(ST_Normalize(geom)))`.

Normalising means the same shape written with a different start vertex is "unchanged"; any real vertex change is a change.

## Idempotency and audit

### Idempotency (three layers)

- `load` is a no-op for the same `batch_id` + same file hash. The same `batch_id` with different content is refused.
- Promoting already-applied data yields unchanged: no row, version, or timestamp moves.
- The upsert `WHERE` guard means even a race could not write an older or identical row.

### Audit trail

| Where | What |
| --- | --- |
| `iris.promotion_runs` | counts per run |
| `iris.rejected_parcels` | reason, detail, and the raw staged row (JSON) |
| `iris.parcel_change_log` | old/new hash, old/new date, changed columns |
| `manifests/*.json` | per-run manifest: input file SHA-256, counts, rejects by reason, every change, the rules applied |

Sample manifests from the demo are committed in `manifests/`.

## Tests

33 automated tests (`pytest`) cover the highest-risk conditions:

- Insert / update / rerun: first load, identical rerun (no rows, versions, or timestamps move), rerun of a corrections batch, batch-id reuse with different content refused.
- Update logic: geometry + attribute change, geometry-only change, same shape with a different start vertex is unchanged, older never overwrites newer, same-date conflict, higher authority wins, freshness confirmation.
- Safety: missing optional values are not invented, core rows absent from a batch are never deleted, failed promotion rolls back completely.
- Country isolation: same ref in two countries, updating one leaves the other untouched, the database itself blocks duplicate keys / NULL country / unknown country.
- Rejections: 10 parametrised bad-row cases plus missing geometry, each rejected with the right reason and kept inspectable.
- Determinism: in-batch duplicates resolve consistently; result is identical regardless of row order.
- Manifest: counts, reasons, file hash.

I also deliberately broke the SQL five ways (removed the stale check, made the key not country-scoped, let same-freshness conflicts overwrite, removed geometry normalisation, removed freshness confirmation). The tests caught every one.

## Task checklist

| Requirement | Where / how |
| --- | --- |
| Staging and core example for parcels | `iris.staging_parcels`, `iris.core_parcels` |
| Idempotent upsert, country-scoped key | `PRIMARY KEY (country_code, parcel_ref)`; `INSERT .. ON CONFLICT DO UPDATE` |
| Update attributes and geom when newer/authoritative `(source_date, source_rank)` rule | implemented in promotion logic |
| Inserted / updated / unchanged / rejected counts | `iris.promotion_runs`, manifest counts |
| Rerun safety proven by tests | `tests/test_promotion.py` |
| Not `DELETE ALL + INSERT ALL` | never deletes; tested |
| Acceptance 1: second identical run, no duplicates | `test_identical_rerun_creates_no_duplicates_and_no_writes` |
| Acceptance 2: changed geometry/attributes update core | `test_newer_record_updates_geometry_and_attributes`, `test_geometry_only_change_is_detected` |
| Acceptance 3: country collisions impossible | composite PK + FK + NOT NULL; `test_database_itself_blocks_country_collisions_and_nulls` |
| Acceptance 4: rejected records inspectable | `iris.rejected_parcels`, `inspect-rejects`, manifest |
| Python 3.12+, PostgreSQL 16+, PostGIS 3.4+, column `geom` | yes |

### Deliverables

- SQL + Python
- tests
- manifest
- `migrations/`, `src/`, `tests/`, `manifests/`
- small deterministic fixtures committed `fixtures/` (4 CSVs)
- no secrets, paid APIs, or proprietary data

### Runs from a clean checkout via documented commands

See [Quick start](#quick-start).

## Assumptions and simplifications

### Simplification

- CSV + WKT fixtures stand in for a remote source
- A country-aware adapter writes to staging.
- Only EPSG:4326 accepted
- Allow a declared source CRS and transform, recording CRS and accuracy.
- `source_rank` set per batch
- Per-source authority table and per-row rank.
- Same freshness + different content is rejected
- Keep; route to a review queue with ownership rules.
- Geometry change is exact (vertex-level)
- Optional tolerance/snap policy decided by the data owner.
- No soft-delete / tombstones
- "Parcel disappeared" needs its own explicit contract.
- Countries seeded (`DE`, `FR`, `NL`) as reference data only
- Load the full country list from a managed reference source.
- One promotion at a time (global advisory lock)
- Lock per country; batch very large loads.
- Parcels only; no downstream outputs
- Prospecting outputs must carry the project's indicative-material wording.

## Troubleshooting

### Problem: cannot connect to the database

Fix: Is Docker Desktop running? Run `docker compose up -d` and wait ~20 s.

### Problem: database `iris` does not exist

Fix: You are probably reaching another PostgreSQL on port 5432. Change the compose port to `5433:5432` and set both `IRIS_*_URL` variables to port `5433`.

### Problem: database `iris_test` does not exist

Fix: The test suite creates it automatically; otherwise run:

```bash
docker exec iris-db psql -U postgres -c "CREATE DATABASE iris_test;"
```

### Problem: extension "postgis" is not available

Fix: The server has no PostGIS. Use the Docker image from this repo.

### Problem: `pytest` is not recognized

Fix: Activate the virtual environment first, or use `python -m pytest`.

## Project layout

```text
migrations/001_parcel_promotion.sql   schema + iris.promote_parcels() + helper functions
src/iris_promotion/                   CLI, loader, manifest writer
fixtures/                             4 small deterministic CSVs
tests/                                33 tests (pytest)
manifests/                            sample manifests from the demo run
docker-compose.yml, docker/init.sql   PostgreSQL 16 + PostGIS 3.4 for local use
WINDOWS_SETUP.md                      step-by-step Windows guide
```


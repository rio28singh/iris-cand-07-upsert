"""Load a CSV into staging. Staging is append-only and idempotent per batch."""
from __future__ import annotations

import csv
import hashlib
from pathlib import Path

import psycopg

REQUIRED = ["country_code", "parcel_ref", "source_date", "srid", "land_use", "area_m2", "geom_wkt"]


class BatchConflict(Exception):
    """Same batch_id already staged with different file content."""


def file_sha256(path: Path) -> str:
    return hashlib.sha256(path.read_bytes()).hexdigest()


def load_csv(conn: psycopg.Connection, path: Path, batch_id: str, source_rank: int = 0) -> dict:
    """Stage `path` under `batch_id`.

    Re-loading the identical file under the same batch_id is a no-op (so a full
    re-run never double-stages rows). Same batch_id + different content is an error.
    """
    path = Path(path)
    digest = file_sha256(path)
    existing = conn.execute(
        "SELECT file_sha256, row_count FROM iris.staging_batches WHERE batch_id = %s", (batch_id,)
    ).fetchone()
    if existing:
        if existing[0] != digest:
            raise BatchConflict(f"batch_id {batch_id!r} already staged from different content")
        return {"batch_id": batch_id, "rows": existing[1], "already_staged": True, "sha256": digest}

    with path.open(newline="", encoding="utf-8") as fh:
        reader = csv.DictReader(fh)
        missing = [c for c in REQUIRED if c not in (reader.fieldnames or [])]
        if missing:
            raise ValueError(f"CSV missing required columns: {missing}")
        rows = list(reader)

    for i, row in enumerate(rows, start=2):  # line 1 is the header
        if not (row["country_code"] or "").strip():
            raise ValueError(f"{path.name} line {i}: country_code is mandatory (country-scoped data)")

    conn.execute(
        "INSERT INTO iris.staging_batches (batch_id, source_file, file_sha256, source_rank, row_count)"
        " VALUES (%s, %s, %s, %s, %s)",
        (batch_id, path.name, digest, source_rank, len(rows)),
    )
    with conn.cursor() as cur:
        cur.executemany(
            "INSERT INTO iris.staging_parcels (batch_id, country_code, parcel_ref, source_date,"
            " source_rank, srid, land_use, area_m2, geom_wkt) VALUES (%s,%s,%s,%s,%s,%s,%s,%s,%s)",
            [
                (batch_id, r["country_code"], r["parcel_ref"], r["source_date"], source_rank,
                 r["srid"], r["land_use"], r["area_m2"], r["geom_wkt"])
                for r in rows
            ],
        )
    conn.commit()
    return {"batch_id": batch_id, "rows": len(rows), "already_staged": False, "sha256": digest}

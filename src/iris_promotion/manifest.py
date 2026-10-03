"""Promotion manifest: a JSON audit record of exactly what a run did."""
from __future__ import annotations

import json
from pathlib import Path

import psycopg

from . import __version__


def build_manifest(conn: psycopg.Connection, run_id: int) -> dict:
    run = conn.execute(
        "SELECT r.run_id, r.batch_id, r.started_at, r.finished_at, r.staged_count, r.inserted,"
        " r.updated, r.unchanged, r.rejected, b.source_file, b.file_sha256, b.source_rank"
        " FROM iris.promotion_runs r JOIN iris.staging_batches b USING (batch_id) WHERE r.run_id = %s",
        (run_id,),
    ).fetchone()
    if run is None:
        raise KeyError(f"unknown run_id {run_id}")
    reasons = conn.execute(
        "SELECT reason, count(*) FROM iris.rejected_parcels WHERE run_id = %s GROUP BY reason ORDER BY reason",
        (run_id,),
    ).fetchall()
    changes = conn.execute(
        "SELECT country_code, parcel_ref, change_type, changed_columns FROM iris.parcel_change_log"
        " WHERE run_id = %s ORDER BY country_code, parcel_ref", (run_id,),
    ).fetchall()
    rejects = conn.execute(
        "SELECT staging_id, country_code, parcel_ref, reason FROM iris.rejected_parcels"
        " WHERE run_id = %s ORDER BY staging_id", (run_id,),
    ).fetchall()
    return {
        "manifest_version": 1,
        "tool_version": __version__,
        "run_id": run[0],
        "batch_id": run[1],
        "started_at": run[2].isoformat(),
        "finished_at": run[3].isoformat() if run[3] else None,
        "input": {"file": run[9], "sha256": run[10], "source_rank": run[11]},
        "counts": {
            "staged": run[4], "inserted": run[5], "updated": run[6],
            "unchanged": run[7], "rejected": run[8],
            "invariant_holds": run[4] == run[5] + run[6] + run[7] + run[8],
        },
        "rejected_by_reason": {r: n for r, n in reasons},
        "rules": {
            "natural_key": ["country_code", "parcel_ref"],
            "freshness": "update only if (source_date, source_rank) is strictly greater than core",
            "change_detection": "sha256(land_use, area_m2, normalised EWKB geom)",
            "geometry": "geom MultiPolygon EPSG:4326, must be valid; never repaired",
        },
        "changes": [
            {"country_code": c, "parcel_ref": p, "type": t, "changed_columns": cols}
            for c, p, t, cols in changes
        ],
        "rejects": [
            {"staging_id": s, "country_code": c, "parcel_ref": p, "reason": r}
            for s, c, p, r in rejects
        ],
    }


def write_manifest(manifest: dict, path: Path) -> Path:
    path = Path(path)
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(manifest, indent=2, sort_keys=True) + "\n", encoding="utf-8")
    return path

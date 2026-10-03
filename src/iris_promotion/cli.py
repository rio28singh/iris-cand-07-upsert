"""Command line:  iris-promote {migrate|demo|load|promote|run|inspect-rejects}"""
from __future__ import annotations

import argparse
import json
import sys
from pathlib import Path

import psycopg

from . import db, loader, manifest


def _promote(conn, batch_id: str, manifest_path: Path | None) -> dict:
    run_id = conn.execute("SELECT run_id FROM iris.promote_parcels(%s)", (batch_id,)).fetchone()[0]
    conn.commit()
    m = manifest.build_manifest(conn, run_id)
    if manifest_path:
        manifest.write_manifest(m, manifest_path)
    return m


DEMO_STEPS = [  # (csv, batch_id, source_rank, manifest name, what it shows)
    ("batch1_initial.csv", "b1", 0, "run1_b1_initial", "first load: inserts + rejects bad rows"),
    ("batch1_initial.csv", "b1", 0, "run2_b1_rerun", "IDENTICAL RERUN: nothing changes"),
    ("batch2_corrections.csv", "b2", 0, "run3_b2_corrections", "corrections: update + insert + unchanged"),
    ("batch2_corrections.csv", "b2", 0, "run4_b2_rerun", "rerun of corrections: nothing changes"),
    ("batch3_stale_and_conflict.csv", "b3", 0, "run5_b3_stale_conflict", "old data + same-date conflict: rejected"),
    ("batch4_authoritative.csv", "b4", 10, "run6_b4_authoritative", "higher-authority source wins the conflict"),
]


def _demo(conn) -> None:
    print("NOTE: demo resets the 'iris' schema in this database, then replays 6 runs.\n")
    conn.execute("DROP SCHEMA IF EXISTS iris CASCADE")
    conn.commit()
    db.migrate(conn)
    for csv_name, batch, rank, name, why in DEMO_STEPS:
        loader.load_csv(conn, db.FIXTURES_DIR / csv_name, batch, rank)
        m = _promote(conn, batch, db.MANIFESTS_DIR / f"{name}.json")
        c = m["counts"]
        print(f"{name}: {why}\n    inserted={c['inserted']} updated={c['updated']} "
              f"unchanged={c['unchanged']} rejected={c['rejected']} (staged={c['staged']}) "
              f"{m['rejected_by_reason'] or ''}")
    print("\nFinal core_parcels:")
    print(f"{'country':8}{'parcel':8}{'land_use':14}{'source_date':13}{'rank':6}version")
    for r in conn.execute("SELECT country_code, parcel_ref, land_use, source_date, source_rank, version"
                          " FROM iris.core_parcels ORDER BY 1, 2"):
        print(f"{r[0]:8}{r[1]:8}{str(r[2]):14}{str(r[3]):13}{r[4]:<6}{r[5]}")
    print(f"\nManifests written to: {db.MANIFESTS_DIR}")


def main(argv: list[str] | None = None) -> int:
    p = argparse.ArgumentParser(prog="iris-promote", description=__doc__)
    p.add_argument("--dsn", help="PostgreSQL URL (default: $IRIS_DATABASE_URL)")
    sub = p.add_subparsers(dest="cmd", required=True)

    sub.add_parser("migrate", help="apply SQL migrations")
    sub.add_parser("demo", help="reset schema and replay the 6-run demo story")

    s = sub.add_parser("load", help="stage a CSV")
    s.add_argument("csv", type=Path); s.add_argument("--batch-id", required=True)
    s.add_argument("--source-rank", type=int, default=0)

    s = sub.add_parser("promote", help="promote a staged batch to core")
    s.add_argument("--batch-id", required=True); s.add_argument("--manifest", type=Path)

    s = sub.add_parser("run", help="load + promote in one step (safe to repeat)")
    s.add_argument("csv", type=Path); s.add_argument("--batch-id", required=True)
    s.add_argument("--source-rank", type=int, default=0); s.add_argument("--manifest", type=Path)

    s = sub.add_parser("inspect-rejects", help="list rejected rows for a run")
    s.add_argument("--run-id", type=int, required=True)

    a = p.parse_args(argv)
    try:
        conn_ctx = db.connect(a.dsn)
    except psycopg.OperationalError as exc:
        print("ERROR: cannot connect to the database.\n"
              "  - Is PostgreSQL running? (Docker: 'docker compose up -d')\n"
              "  - Is the URL right? Currently: " + (a.dsn or db.get_dsn()) + "\n"
              "  - Set another one with:  set IRIS_DATABASE_URL=postgresql://user:pass@host:5432/dbname\n"
              f"Details: {str(exc).strip().splitlines()[0]}", file=sys.stderr)
        return 2
    with conn_ctx as conn:
        if a.cmd == "migrate":
            print("applied:", ", ".join(db.migrate(conn)))
        elif a.cmd == "demo":
            _demo(conn)
        elif a.cmd == "load":
            print(json.dumps(loader.load_csv(conn, a.csv, a.batch_id, a.source_rank)))
        elif a.cmd == "promote":
            m = _promote(conn, a.batch_id, a.manifest)
            print(json.dumps(m["counts"]))
        elif a.cmd == "run":
            info = loader.load_csv(conn, a.csv, a.batch_id, a.source_rank)
            m = _promote(conn, a.batch_id, a.manifest)
            print(json.dumps({"staged": info, "counts": m["counts"],
                              "rejected_by_reason": m["rejected_by_reason"]}))
        elif a.cmd == "inspect-rejects":
            for row in conn.execute(
                "SELECT staging_id, country_code, parcel_ref, reason, coalesce(detail,'')"
                " FROM iris.rejected_parcels WHERE run_id=%s ORDER BY staging_id", (a.run_id,)):
                print("\t".join(str(x) for x in row))
    return 0


if __name__ == "__main__":
    sys.exit(main())

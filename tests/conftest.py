import csv
import os
from pathlib import Path

import psycopg
import pytest
from psycopg import sql
from psycopg.conninfo import conninfo_to_dict, make_conninfo

from iris_promotion import db, loader, manifest

FIXTURES = Path(__file__).resolve().parents[1] / "fixtures"
TEST_DSN = os.environ.get("IRIS_TEST_DATABASE_URL",
                          "postgresql://postgres:postgres@localhost:5432/iris_test")
HEADER = ["country_code", "parcel_ref", "source_date", "srid", "land_use", "area_m2", "geom_wkt"]


def square(x, y, size=0.001):
    return (f"POLYGON(({x} {y},{x+size} {y},{x+size} {y+size},{x} {y+size},{x} {y}))")

@pytest.fixture(scope="session", autouse=True)
def _ensure_test_database():
    """Create the test database if it is missing (Docker only auto-creates it on first start)."""
    try:
        psycopg.connect(TEST_DSN).close()
        return
    except psycopg.OperationalError as exc:
        if "does not exist" not in str(exc):
            raise
    name = conninfo_to_dict(TEST_DSN)["dbname"]
    with psycopg.connect(make_conninfo(TEST_DSN, dbname="postgres"), autocommit=True) as admin:
        admin.execute(sql.SQL("CREATE DATABASE {}").format(sql.Identifier(name)))

@pytest.fixture()
def conn():
    """Fresh `iris` schema for every test; the DB itself must have PostGIS available."""
    c = psycopg.connect(TEST_DSN, autocommit=False)
    c.execute("DROP SCHEMA IF EXISTS iris CASCADE")
    c.commit()
    db.migrate(c)
    yield c
    c.rollback()
    c.close()


class Runner:
    def __init__(self, conn, tmp_path):
        self.conn, self.tmp, self.n = conn, tmp_path, 0

    def promote_rows(self, rows, rank=0, batch_id=None):
        """rows: list of dicts (missing keys default to ''). Returns the run row as a dict."""
        self.n += 1
        batch_id = batch_id or f"t{self.n}"
        path = self.tmp / f"{batch_id}.csv"
        with path.open("w", newline="") as fh:
            w = csv.DictWriter(fh, fieldnames=HEADER)
            w.writeheader()
            for r in rows:
                w.writerow({k: r.get(k, "") for k in HEADER})
        return self.promote_file(path, rank, batch_id)

    def promote_file(self, path, rank=0, batch_id=None):
        self.n += 1
        batch_id = batch_id or f"f{self.n}"
        loader.load_csv(self.conn, path, batch_id, rank)
        row = self.conn.execute("SELECT * FROM iris.promote_parcels(%s)", (batch_id,)).fetchone()
        self.conn.commit()
        cols = ["run_id", "batch_id", "started_at", "finished_at", "staged", "inserted",
                "updated", "unchanged", "rejected"]
        return dict(zip(cols, row))

    def core(self):
        return {(r[0], r[1]): r for r in self.conn.execute(
            "SELECT country_code, parcel_ref, land_use, area_m2, source_date, source_rank, version,"
            " ST_AsText(geom) FROM iris.core_parcels")}

    def count(self, table="iris.core_parcels"):
        return self.conn.execute(f"SELECT count(*) FROM {table}").fetchone()[0]


@pytest.fixture()
def run(conn, tmp_path):
    return Runner(conn, tmp_path)


def parcel(cc="DE", ref="P-1", date="2024-01-01", land_use="agri", area="100", wkt=None, srid="4326"):
    return dict(country_code=cc, parcel_ref=ref, source_date=date, srid=srid,
                land_use=land_use, area_m2=area, geom_wkt=wkt or square(10, 50))

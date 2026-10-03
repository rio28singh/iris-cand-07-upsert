"""Highest-risk correctness conditions for staging -> core promotion."""
import json

import psycopg
import pytest

from conftest import FIXTURES, parcel, square
from iris_promotion import manifest


def assert_invariant(r):
    assert r["staged"] == r["inserted"] + r["updated"] + r["unchanged"] + r["rejected"]


# ------------------------------------------------------------------ insert
def test_first_run_inserts_valid_rows_and_rejects_bad_ones(run):
    r = run.promote_file(FIXTURES / "batch1_initial.csv")
    assert (r["inserted"], r["updated"], r["unchanged"], r["rejected"]) == (3, 0, 0, 5)
    assert_invariant(r)
    assert run.count() == 3


# ------------------------------------------------------------- idempotency
def test_identical_rerun_creates_no_duplicates_and_no_writes(run, conn):
    run.promote_file(FIXTURES / "batch1_initial.csv", batch_id="b1")
    before = conn.execute("SELECT country_code, parcel_ref, version, updated_at, last_run_id,"
                          " content_hash FROM iris.core_parcels ORDER BY 1,2").fetchall()
    r2 = run.promote_file(FIXTURES / "batch1_initial.csv", batch_id="b1")   # same file, same batch
    after = conn.execute("SELECT country_code, parcel_ref, version, updated_at, last_run_id,"
                         " content_hash FROM iris.core_parcels ORDER BY 1,2").fetchall()
    assert (r2["inserted"], r2["updated"], r2["unchanged"]) == (0, 0, 3)
    assert run.count() == 3
    assert before == after                      # not even version/updated_at/last_run_id moved
    assert conn.execute("SELECT count(*) FROM iris.parcel_change_log WHERE run_id=%s",
                        (r2["run_id"],)).fetchone()[0] == 0
    assert_invariant(r2)


def test_rerun_of_a_new_batch_with_same_content_is_idempotent(run):
    """Different batch_id, identical rows: still no duplicates, no updates."""
    run.promote_rows([parcel()])
    r = run.promote_rows([parcel()])
    assert (r["inserted"], r["updated"], r["unchanged"]) == (0, 0, 1)
    assert run.count() == 1


def test_corrections_batch_rerun_is_idempotent(run):
    run.promote_file(FIXTURES / "batch1_initial.csv")
    run.promote_file(FIXTURES / "batch2_corrections.csv", batch_id="b2")
    snapshot = run.core()
    r = run.promote_file(FIXTURES / "batch2_corrections.csv", batch_id="b2")
    assert (r["inserted"], r["updated"], r["rejected"]) == (0, 0, 0)
    assert run.core() == snapshot


def test_batch_id_reuse_with_different_content_is_refused(run, tmp_path):
    from iris_promotion.loader import BatchConflict
    run.promote_rows([parcel()], batch_id="same")
    with pytest.raises(BatchConflict):
        run.promote_rows([parcel(land_use="changed")], batch_id="same")


# ------------------------------------------------------------------ update
def test_newer_record_updates_geometry_and_attributes(run, conn):
    run.promote_rows([parcel(date="2024-01-01", land_use="agri", wkt=square(10, 50))])
    r = run.promote_rows([parcel(date="2024-06-01", land_use="residential", area="150",
                                 wkt=square(10, 50, 0.002))])
    assert (r["inserted"], r["updated"]) == (0, 1)
    row = run.core()[("DE", "P-1")]
    assert row[2] == "residential" and float(row[3]) == 150.0 and row[6] == 2
    assert row[7] == square(10, 50, 0.002).replace("POLYGON((", "MULTIPOLYGON(((").replace("))", ")))")
    cols = conn.execute("SELECT changed_columns FROM iris.parcel_change_log WHERE run_id=%s",
                        (r["run_id"],)).fetchone()[0]
    assert sorted(cols) == ["area_m2", "geom", "land_use"]


def test_geometry_only_change_is_detected(run, conn):
    run.promote_rows([parcel(date="2024-01-01")])
    r = run.promote_rows([parcel(date="2024-02-01", wkt=square(10, 50, 0.0015))])
    assert r["updated"] == 1
    assert conn.execute("SELECT changed_columns FROM iris.parcel_change_log WHERE run_id=%s",
                        (r["run_id"],)).fetchone()[0] == ["geom"]


def test_same_shape_with_different_vertex_start_is_unchanged(run):
    run.promote_rows([parcel(wkt="POLYGON((0 0,0 1,1 1,1 0,0 0))")])
    r = run.promote_rows([parcel(date="2024-02-01", wkt="POLYGON((1 1,1 0,0 0,0 1,1 1))")])
    assert (r["updated"], r["unchanged"]) == (0, 1)


def test_older_record_never_overwrites_newer_and_is_inspectable(run, conn):
    run.promote_rows([parcel(date="2024-06-01", land_use="new")])
    r = run.promote_rows([parcel(date="2023-01-01", land_use="old")])
    assert (r["updated"], r["rejected"]) == (0, 1)
    assert run.core()[("DE", "P-1")][2] == "new"
    reason, raw = conn.execute("SELECT reason, raw_row FROM iris.rejected_parcels WHERE run_id=%s",
                               (r["run_id"],)).fetchone()
    assert reason == "STALE_SOURCE" and raw["land_use"] == "old"


def test_same_date_conflict_is_rejected_but_higher_authority_wins(run):
    run.promote_rows([parcel(date="2024-06-01", land_use="a")], rank=0)
    r = run.promote_rows([parcel(date="2024-06-01", land_use="b")], rank=0)
    assert r["rejected"] == 1 and run.core()[("DE", "P-1")][2] == "a"
    r = run.promote_rows([parcel(date="2024-06-01", land_use="b")], rank=10)
    assert r["updated"] == 1 and run.core()[("DE", "P-1")][2] == "b"
    r = run.promote_rows([parcel(date="2024-06-01", land_use="c")], rank=5)     # lower authority
    assert r["rejected"] == 1 and run.core()[("DE", "P-1")][2] == "b"


def test_unchanged_but_newer_confirmation_refreshes_freshness_only(run):
    run.promote_rows([parcel(date="2024-01-01", land_use="a")])
    run.promote_rows([parcel(date="2024-06-01", land_use="a")])          # source re-confirms
    row = run.core()[("DE", "P-1")]
    assert row[4].isoformat() == "2024-06-01" and row[6] == 1            # date moved, version did not
    r = run.promote_rows([parcel(date="2024-06-01", land_use="b")])      # now a true conflict
    assert r["rejected"] == 1


def test_missing_optional_attribute_is_not_invented(run):
    run.promote_rows([parcel(area="", land_use="")])
    row = run.core()[("DE", "P-1")]
    assert row[3] is None and row[2] == ""        # area stays NULL, nothing fabricated


def test_promotion_never_deletes_core_rows_absent_from_batch(run):
    run.promote_rows([parcel(ref="A"), parcel(ref="B")])
    run.promote_rows([parcel(ref="A", date="2024-02-01")])
    assert {k[1] for k in run.core()} == {"A", "B"}


# ---------------------------------------------------- country isolation
def test_same_ref_in_two_countries_never_collides(run):
    r = run.promote_rows([parcel(cc="DE", ref="P-1", land_use="de"),
                          parcel(cc="FR", ref="P-1", land_use="fr")])
    assert r["inserted"] == 2 and run.count() == 2
    run.promote_rows([parcel(cc="DE", ref="P-1", date="2024-09-01", land_use="de2")])
    core = run.core()
    assert core[("DE", "P-1")][2] == "de2" and core[("FR", "P-1")][2] == "fr"   # FR untouched


def test_database_itself_blocks_country_collisions_and_nulls(conn, run):
    run.promote_rows([parcel()])
    base = ("INSERT INTO iris.core_parcels (country_code, parcel_ref, source_date, source_rank,"
            " geom, content_hash, last_run_id) VALUES (%s, %s, '2024-01-01', 0,"
            " ST_Multi(ST_GeomFromText(%s, 4326)), 'h', 1)")
    for args, exc in [(("DE", "P-1", square(1, 1)), psycopg.errors.UniqueViolation),
                      ((None, "X", square(1, 1)), psycopg.errors.NotNullViolation),
                      (("ZZ", "X", square(1, 1)), psycopg.errors.ForeignKeyViolation)]:
        with pytest.raises(exc):
            conn.execute(base, args)
        conn.rollback()


def test_country_code_is_normalised_but_unknown_is_rejected(run):
    r = run.promote_rows([parcel(cc=" de "), parcel(cc="US", ref="P-2")])
    assert (r["inserted"], r["rejected"]) == (1, 1)
    assert ("DE", "P-1") in run.core()


# ------------------------------------------------------------- rejection
@pytest.mark.parametrize("override,reason", [
    (dict(wkt="POLYGON((0 0,1 1,1 0,0 1,0 0))"), "INVALID_GEOMETRY"),   # bow-tie: never auto-repaired
    (dict(wkt="not wkt"), "INVALID_WKT"),
    (dict(wkt="POINT(1 1)"), "UNSUPPORTED_GEOMETRY_TYPE"),
    (dict(srid="3857"), "UNSUPPORTED_CRS"),
    (dict(srid=""), "MISSING_CRS"),
    (dict(date="2999-01-01"), "FUTURE_SOURCE_DATE"),
    (dict(date="garbage"), "INVALID_SOURCE_DATE"),
    (dict(area="-5"), "INVALID_AREA"),
    (dict(area="abc"), "INVALID_AREA"),
    (dict(ref=" "), "MISSING_PARCEL_REF"),
])
def test_bad_rows_are_rejected_with_reason_and_never_reach_core(run, conn, override, reason):
    p = parcel(**override)
    if "wkt" in override:
        p["geom_wkt"] = override["wkt"]
    r = run.promote_rows([p])
    assert (r["inserted"], r["rejected"]) == (0, 1) and run.count() == 0
    got, raw = conn.execute("SELECT reason, raw_row FROM iris.rejected_parcels").fetchone()
    assert got == reason and raw["country_code"] == "DE"


def test_missing_geometry_rejected_not_invented(run, conn):
    p = parcel(); p["geom_wkt"] = ""
    r = run.promote_rows([p])
    assert r["rejected"] == 1
    assert conn.execute("SELECT reason FROM iris.rejected_parcels").fetchone()[0] == "MISSING_GEOMETRY"


def test_blank_country_code_cannot_even_be_staged(run):
    with pytest.raises(ValueError, match="country_code"):
        run.promote_rows([parcel(cc="")])


def test_polygon_is_stored_as_multipolygon_in_4326(run, conn):
    run.promote_rows([parcel()])
    assert conn.execute("SELECT GeometryType(geom), ST_SRID(geom) FROM iris.core_parcels"
                        ).fetchone() == ("MULTIPOLYGON", 4326)


# ----------------------------------------------------------- determinism
def test_duplicates_inside_a_batch_resolve_deterministically(run, conn):
    rows = [parcel(date="2024-01-01", land_use="old"),
            parcel(date="2024-03-01", land_use="newest"),
            parcel(date="2024-02-01", land_use="middle")]
    r = run.promote_rows(rows)
    assert (r["inserted"], r["rejected"]) == (1, 2)
    assert run.core()[("DE", "P-1")][2] == "newest"
    assert {x[0] for x in conn.execute("SELECT reason FROM iris.rejected_parcels")} == {"SUPERSEDED_IN_BATCH"}


def test_result_does_not_depend_on_row_order(run, conn):
    rows = [parcel(ref=f"R{i}", date=f"2024-0{1 + i % 3}-01", land_use=f"v{i}") for i in range(6)]
    rows += [parcel(ref="R1", date="2024-09-01", land_use="late")]
    run.promote_rows(rows)
    forward = run.core()
    conn.execute("DROP SCHEMA iris CASCADE"); conn.commit()
    from iris_promotion import db
    db.migrate(conn)
    run.promote_rows(list(reversed(rows)), batch_id="rev")
    assert run.core() == forward


# -------------------------------------------------------------- manifest
def test_manifest_records_counts_reasons_and_file_hash(run, conn, tmp_path):
    r = run.promote_file(FIXTURES / "batch1_initial.csv", batch_id="b1")
    m = manifest.build_manifest(conn, r["run_id"])
    assert m["counts"] == {"staged": 8, "inserted": 3, "updated": 0, "unchanged": 0,
                           "rejected": 5, "invariant_holds": True}
    assert m["rejected_by_reason"]["INVALID_GEOMETRY"] == 1
    assert len(m["input"]["sha256"]) == 64 and m["rules"]["natural_key"] == ["country_code", "parcel_ref"]
    out = manifest.write_manifest(m, tmp_path / "m.json")
    assert json.loads(out.read_text())["run_id"] == r["run_id"]


def test_failed_promotion_rolls_back_completely(run, conn):
    from iris_promotion import loader
    with pytest.raises(psycopg.errors.RaiseException):
        conn.execute("SELECT iris.promote_parcels('does-not-exist')")
    conn.rollback()
    assert run.count("iris.promotion_runs") == 0

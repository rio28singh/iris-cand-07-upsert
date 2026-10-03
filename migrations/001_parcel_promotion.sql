-- IRIS-CAND-07 : Safe staging-to-core upsert promotion for parcels.
-- Requires PostgreSQL 16+ and PostGIS 3.4+. Idempotent: safe to apply repeatedly.
--
-- Data contracts (explicit, enforced here):
--   * Natural key            : (country_code, parcel_ref)  -- ALWAYS country-scoped
--   * Geometry               : column `geom`, MultiPolygon, EPSG:4326, must be valid
--   * Freshness              : (source_date, source_rank) compared lexicographically
--   * Missing values         : stay NULL (area_m2, land_use) or the row is rejected
--                              (geometry, CRS, date). Nothing is invented or repaired.

CREATE EXTENSION IF NOT EXISTS postgis;
CREATE SCHEMA IF NOT EXISTS iris;

-- ---------------------------------------------------------------- reference
CREATE TABLE IF NOT EXISTS iris.countries (
    country_code char(2) PRIMARY KEY CHECK (country_code ~ '^[A-Z]{2}$'),
    name         text    NOT NULL
);
INSERT INTO iris.countries (country_code, name) VALUES
    ('DE', 'Germany'), ('FR', 'France'), ('NL', 'Netherlands')
ON CONFLICT (country_code) DO NOTHING;   -- reference seed only; not business data

-- ------------------------------------------------------------------ staging
-- Staging is deliberately loose (text) so bad source rows can be LOADED and then
-- REJECTED with a reason, instead of crashing the load. Rows are never edited.
CREATE TABLE IF NOT EXISTS iris.staging_batches (
    batch_id    text PRIMARY KEY,
    source_file text        NOT NULL,
    file_sha256 text        NOT NULL,
    source_rank integer     NOT NULL DEFAULT 0,   -- authority of the source system
    row_count   integer     NOT NULL,
    loaded_at   timestamptz NOT NULL DEFAULT now()
);

CREATE TABLE IF NOT EXISTS iris.staging_parcels (
    staging_id  bigserial PRIMARY KEY,
    batch_id    text    NOT NULL REFERENCES iris.staging_batches (batch_id),
    country_code text   NOT NULL CHECK (btrim(country_code) <> ''),
    parcel_ref  text,
    source_date text,
    source_rank integer NOT NULL DEFAULT 0,
    srid        text,
    land_use    text,
    area_m2     text,
    geom_wkt    text
);
CREATE INDEX IF NOT EXISTS staging_parcels_batch_idx ON iris.staging_parcels (batch_id);

-- --------------------------------------------------------------------- core
CREATE TABLE IF NOT EXISTS iris.core_parcels (
    country_code char(2) NOT NULL REFERENCES iris.countries (country_code),
    parcel_ref   text    NOT NULL CHECK (btrim(parcel_ref) <> ''),
    land_use     text,
    area_m2      numeric CHECK (area_m2 IS NULL OR area_m2 >= 0),   -- square metres
    source_date  date    NOT NULL,
    source_rank  integer NOT NULL,
    geom         geometry(MultiPolygon, 4326) NOT NULL CHECK (ST_IsValid(geom)),
    content_hash text    NOT NULL,
    version      integer NOT NULL DEFAULT 1,
    created_at   timestamptz NOT NULL DEFAULT now(),
    updated_at   timestamptz NOT NULL DEFAULT now(),
    last_run_id  bigint  NOT NULL,
    -- Country-scoped natural key: the same parcel_ref in two countries can never collide.
    PRIMARY KEY (country_code, parcel_ref)
);
CREATE INDEX IF NOT EXISTS core_parcels_geom_gix ON iris.core_parcels USING gist (geom);

-- -------------------------------------------------------------------- audit
CREATE TABLE IF NOT EXISTS iris.promotion_runs (
    run_id        bigserial PRIMARY KEY,
    batch_id      text        NOT NULL REFERENCES iris.staging_batches (batch_id),
    started_at    timestamptz NOT NULL DEFAULT clock_timestamp(),
    finished_at   timestamptz,
    staged_count  integer,
    inserted      integer,
    updated       integer,
    unchanged     integer,
    rejected      integer,
    CHECK (finished_at IS NULL OR staged_count = inserted + updated + unchanged + rejected)
);

CREATE TABLE IF NOT EXISTS iris.rejected_parcels (
    reject_id    bigserial PRIMARY KEY,
    run_id       bigint NOT NULL REFERENCES iris.promotion_runs (run_id),
    staging_id   bigint NOT NULL REFERENCES iris.staging_parcels (staging_id),
    country_code text   NOT NULL,
    parcel_ref   text,
    reason       text   NOT NULL,
    detail       text,
    raw_row      jsonb  NOT NULL
);
CREATE INDEX IF NOT EXISTS rejected_parcels_run_idx ON iris.rejected_parcels (run_id, reason);

CREATE TABLE IF NOT EXISTS iris.parcel_change_log (
    change_id        bigserial PRIMARY KEY,
    run_id           bigint NOT NULL REFERENCES iris.promotion_runs (run_id),
    staging_id       bigint NOT NULL REFERENCES iris.staging_parcels (staging_id),
    country_code     char(2) NOT NULL,
    parcel_ref       text    NOT NULL,
    change_type      text    NOT NULL CHECK (change_type IN ('INSERTED', 'UPDATED')),
    changed_columns  text[]  NOT NULL,
    old_hash         text,
    new_hash         text    NOT NULL,
    old_source_date  date,
    new_source_date  date    NOT NULL
);
CREATE INDEX IF NOT EXISTS parcel_change_log_key_idx
    ON iris.parcel_change_log (country_code, parcel_ref);

-- ------------------------------------------------------------ safe parsers
-- Return NULL instead of raising, so validation can classify the failure.
CREATE OR REPLACE FUNCTION iris.try_date(t text) RETURNS date
LANGUAGE plpgsql IMMUTABLE AS $$
BEGIN RETURN t::date; EXCEPTION WHEN others THEN RETURN NULL; END $$;

CREATE OR REPLACE FUNCTION iris.try_numeric(t text) RETURNS numeric
LANGUAGE plpgsql IMMUTABLE AS $$
BEGIN RETURN t::numeric; EXCEPTION WHEN others THEN RETURN NULL; END $$;

CREATE OR REPLACE FUNCTION iris.try_int(t text) RETURNS integer
LANGUAGE plpgsql IMMUTABLE AS $$
BEGIN RETURN t::integer; EXCEPTION WHEN others THEN RETURN NULL; END $$;

CREATE OR REPLACE FUNCTION iris.try_geom(wkt text, srid integer) RETURNS geometry
LANGUAGE plpgsql IMMUTABLE AS $$
BEGIN RETURN ST_GeomFromText(wkt, srid); EXCEPTION WHEN others THEN RETURN NULL; END $$;

-- Change detection fingerprint. Geometry is normalised (vertex/ring order) so the
-- same shape written differently is "unchanged"; any real vertex change differs.
CREATE OR REPLACE FUNCTION iris.parcel_content_hash(
    p_land_use text, p_area_m2 numeric, p_geom geometry
) RETURNS text
LANGUAGE sql IMMUTABLE AS $$
    SELECT encode(sha256(convert_to(
        format('%L|%L|%s', p_land_use, p_area_m2,
               encode(ST_AsEWKB(ST_Normalize(p_geom)), 'hex')), 'UTF8')), 'hex')
$$;

-- --------------------------------------------------------------- promotion
-- Promotes ONE staged batch into iris.core_parcels. Atomic (single transaction):
-- either the whole run, its counts and its audit rows commit, or nothing does.
-- Deterministic: same staged rows + same core state => same result, regardless of
-- the physical order of staged rows.
CREATE OR REPLACE FUNCTION iris.promote_parcels(p_batch_id text)
RETURNS iris.promotion_runs
LANGUAGE plpgsql AS $$
DECLARE
    v_run     iris.promotion_runs;
    v_staged  integer;
    v_ins     integer;
    v_upd     integer;
    v_unch    integer;
    v_rej     integer;
BEGIN
    IF NOT EXISTS (SELECT 1 FROM iris.staging_batches WHERE batch_id = p_batch_id) THEN
        RAISE EXCEPTION 'Unknown batch_id: %', p_batch_id;
    END IF;

    -- One promotion at a time: removes read-then-write races between runs.
    PERFORM pg_advisory_xact_lock(hashtextextended('iris.promote_parcels', 0));

    INSERT INTO iris.promotion_runs (batch_id) VALUES (p_batch_id) RETURNING * INTO v_run;

    -- 1. Parse + validate every staged row. Nothing is repaired or invented.
    DROP TABLE IF EXISTS _parsed;
    CREATE TEMP TABLE _parsed ON COMMIT DROP AS
    SELECT s.staging_id,
           upper(btrim(s.country_code))                      AS country_code,
           btrim(s.parcel_ref)                               AS parcel_ref,
           iris.try_date(btrim(s.source_date))               AS source_date,
           s.source_rank,
           btrim(s.land_use)                                 AS land_use,
           iris.try_numeric(nullif(btrim(s.area_m2), ''))    AS area_m2,
           (nullif(btrim(s.area_m2), '') IS NOT NULL)        AS area_given,
           iris.try_int(btrim(s.srid))                       AS srid,
           iris.try_geom(nullif(btrim(s.geom_wkt), ''),
                         CASE WHEN iris.try_int(btrim(s.srid)) = 4326 THEN 4326 ELSE 0 END)
                                                             AS geom_raw,
           (nullif(btrim(s.geom_wkt), '') IS NOT NULL)       AS wkt_given
    FROM iris.staging_parcels s
    WHERE s.batch_id = p_batch_id;

    GET DIAGNOSTICS v_staged = ROW_COUNT;

    DROP TABLE IF EXISTS _validated;
    CREATE TEMP TABLE _validated ON COMMIT DROP AS
    SELECT p.*,
           CASE WHEN p.geom_raw IS NOT NULL AND GeometryType(p.geom_raw) IN ('POLYGON', 'MULTIPOLYGON')
                THEN ST_Multi(p.geom_raw) END               AS geom,
           CASE
             WHEN NOT EXISTS (SELECT 1 FROM iris.countries c WHERE c.country_code = p.country_code)
                                                    THEN 'UNKNOWN_COUNTRY'
             WHEN p.parcel_ref IS NULL OR p.parcel_ref = ''
                                                    THEN 'MISSING_PARCEL_REF'
             WHEN p.source_date IS NULL             THEN 'INVALID_SOURCE_DATE'
             WHEN p.source_date > current_date      THEN 'FUTURE_SOURCE_DATE'
             WHEN p.srid IS NULL                    THEN 'MISSING_CRS'
             WHEN p.srid <> 4326                    THEN 'UNSUPPORTED_CRS'
             WHEN NOT p.wkt_given                   THEN 'MISSING_GEOMETRY'
             WHEN p.geom_raw IS NULL                THEN 'INVALID_WKT'
             WHEN GeometryType(p.geom_raw) NOT IN ('POLYGON', 'MULTIPOLYGON')
                                                    THEN 'UNSUPPORTED_GEOMETRY_TYPE'
             WHEN ST_IsEmpty(p.geom_raw)            THEN 'MISSING_GEOMETRY'
             WHEN NOT ST_IsValid(p.geom_raw)        THEN 'INVALID_GEOMETRY'
             WHEN p.area_given AND (p.area_m2 IS NULL OR p.area_m2 < 0)
                                                    THEN 'INVALID_AREA'
           END                                              AS reject_reason
    FROM _parsed p;

    -- 2. Several staged rows for the same key: deterministic winner =
    --    newest source_date, then highest source_rank, then latest staging_id.
    DROP TABLE IF EXISTS _ranked;
    CREATE TEMP TABLE _ranked ON COMMIT DROP AS
    SELECT v.*,
           row_number() OVER (
               PARTITION BY v.country_code, v.parcel_ref
               ORDER BY v.source_date DESC, v.source_rank DESC, v.staging_id DESC) AS rn
    FROM _validated v
    WHERE v.reject_reason IS NULL;

    -- 3. Compare each winner with the current core row.
    DROP TABLE IF EXISTS _plan;
    CREATE TEMP TABLE _plan ON COMMIT DROP AS
    SELECT r.staging_id, r.country_code, r.parcel_ref, r.land_use, r.area_m2,
           r.source_date, r.source_rank, r.geom,
           iris.parcel_content_hash(r.land_use, r.area_m2, r.geom) AS content_hash,
           c.content_hash AS core_hash,
           c.source_date  AS core_date,
           c.land_use     AS core_land_use,
           c.area_m2      AS core_area_m2,
           c.geom         AS core_geom,
           CASE
             WHEN c.country_code IS NULL THEN 'INSERTED'
             WHEN c.content_hash = iris.parcel_content_hash(r.land_use, r.area_m2, r.geom)
                                         THEN 'UNCHANGED'
             WHEN (r.source_date, r.source_rank) > (c.source_date, c.source_rank)
                                         THEN 'UPDATED'
             WHEN (r.source_date, r.source_rank) < (c.source_date, c.source_rank)
                                         THEN 'STALE_SOURCE'
             ELSE 'CONFLICT_SAME_FRESHNESS'
           END AS action
    FROM _ranked r
    LEFT JOIN iris.core_parcels c
           ON c.country_code = r.country_code AND c.parcel_ref = r.parcel_ref
    WHERE r.rn = 1;

    -- 4. Upsert. ON CONFLICT DO UPDATE (never DO NOTHING) with a freshness guard
    --    as a second line of defence: an older row can never overwrite a newer one.
    WITH up AS (
        INSERT INTO iris.core_parcels AS c
               (country_code, parcel_ref, land_use, area_m2, source_date, source_rank,
                geom, content_hash, last_run_id)
        SELECT country_code, parcel_ref, land_use, area_m2, source_date, source_rank,
               geom, content_hash, v_run.run_id
        FROM _plan
        WHERE action IN ('INSERTED', 'UPDATED')
        ORDER BY country_code, parcel_ref
        ON CONFLICT (country_code, parcel_ref) DO UPDATE
           SET land_use     = EXCLUDED.land_use,
               area_m2      = EXCLUDED.area_m2,
               source_date  = EXCLUDED.source_date,
               source_rank  = EXCLUDED.source_rank,
               geom         = EXCLUDED.geom,
               content_hash = EXCLUDED.content_hash,
               version      = c.version + 1,
               updated_at   = now(),
               last_run_id  = EXCLUDED.last_run_id
         WHERE c.content_hash IS DISTINCT FROM EXCLUDED.content_hash
           AND (EXCLUDED.source_date, EXCLUDED.source_rank) > (c.source_date, c.source_rank)
        RETURNING c.country_code, c.parcel_ref
    )
    INSERT INTO iris.parcel_change_log
           (run_id, staging_id, country_code, parcel_ref, change_type, changed_columns,
            old_hash, new_hash, old_source_date, new_source_date)
    SELECT v_run.run_id, p.staging_id, p.country_code, p.parcel_ref, p.action,
           CASE WHEN p.action = 'INSERTED'
                THEN ARRAY['land_use', 'area_m2', 'geom']
                ELSE array_remove(ARRAY[
                       CASE WHEN p.land_use IS DISTINCT FROM p.core_land_use THEN 'land_use' END,
                       CASE WHEN p.area_m2  IS DISTINCT FROM p.core_area_m2  THEN 'area_m2'  END,
                       CASE WHEN NOT ST_OrderingEquals(ST_Normalize(p.geom), ST_Normalize(p.core_geom))
                            THEN 'geom' END], NULL)
           END,
           p.core_hash, p.content_hash, p.core_date, p.source_date
    FROM up
    JOIN _plan p ON p.country_code = up.country_code AND p.parcel_ref = up.parcel_ref;

    -- 4b. Identical content seen again with a strictly newer (date, rank): the source has
    --     CONFIRMED the row is still current. Record that freshness so a later same-dated but
    --     different record is correctly flagged as a conflict. Not a content change: no
    --     version bump, no updated_at change, counted as UNCHANGED. A pure rerun never gets
    --     here because equal freshness is not strictly greater.
    UPDATE iris.core_parcels c
       SET source_date = p.source_date, source_rank = p.source_rank, last_run_id = v_run.run_id
      FROM _plan p
     WHERE p.action = 'UNCHANGED'
       AND c.country_code = p.country_code AND c.parcel_ref = p.parcel_ref
       AND (p.source_date, p.source_rank) > (c.source_date, c.source_rank);

    -- 5. Everything not promoted stays inspectable, with a reason and the raw row.
    INSERT INTO iris.rejected_parcels
           (run_id, staging_id, country_code, parcel_ref, reason, detail, raw_row)
    SELECT v_run.run_id, s.staging_id, s.country_code, s.parcel_ref, x.reason, x.detail, to_jsonb(s)
    FROM (
        SELECT staging_id, reject_reason AS reason, NULL::text AS detail
          FROM _validated WHERE reject_reason IS NOT NULL
        UNION ALL
        SELECT staging_id, 'SUPERSEDED_IN_BATCH',
               'Another staged row for the same key is newer/more authoritative'
          FROM _ranked WHERE rn > 1
        UNION ALL
        SELECT staging_id, action,
               CASE action
                 WHEN 'STALE_SOURCE' THEN
                   'Incoming source_date/rank is older than the core row (' || core_date || ')'
                 ELSE 'Same source_date and rank as core but different content; needs a human'
               END
          FROM _plan WHERE action IN ('STALE_SOURCE', 'CONFLICT_SAME_FRESHNESS')
    ) x
    JOIN iris.staging_parcels s USING (staging_id);

    -- 6. Counts, derived from what actually happened (not from what was planned).
    SELECT count(*) FILTER (WHERE change_type = 'INSERTED'),
           count(*) FILTER (WHERE change_type = 'UPDATED')
      INTO v_ins, v_upd
      FROM iris.parcel_change_log WHERE run_id = v_run.run_id;
    SELECT count(*) INTO v_unch FROM _plan WHERE action = 'UNCHANGED';
    SELECT count(*) INTO v_rej  FROM iris.rejected_parcels WHERE run_id = v_run.run_id;

    UPDATE iris.promotion_runs
       SET finished_at = clock_timestamp(), staged_count = v_staged,
           inserted = v_ins, updated = v_upd, unchanged = v_unch, rejected = v_rej
     WHERE run_id = v_run.run_id
     RETURNING * INTO v_run;

    RETURN v_run;
END $$;

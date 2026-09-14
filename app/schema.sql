-- Apply once to a fresh database with: python -m app.init_db
BEGIN;
CREATE TABLE loads (
    id bigint GENERATED ALWAYS AS IDENTITY PRIMARY KEY,
    filename text NOT NULL,
    sha256 char(64) NOT NULL,
    row_count bigint NOT NULL CHECK (row_count > 0),
    ingested_at timestamptz NOT NULL DEFAULT clock_timestamp()
);

CREATE TABLE constituents (
    id bigint GENERATED ALWAYS AS IDENTITY PRIMARY KEY,
    load_id bigint NOT NULL REFERENCES loads(id),
    source_row bigint NOT NULL CHECK (source_row >= 2),
    index_code text NOT NULL,
    isin text NOT NULL,
    ticker text NOT NULL,
    name text NOT NULL,
    weight numeric NOT NULL CHECK (weight >= 0 AND weight <= 100),
    shares numeric NOT NULL CHECK (shares >= 0 AND shares <> 'NaN'::numeric AND shares <> 'Infinity'::numeric),
    effective_date date NOT NULL,
    UNIQUE (load_id, source_row)
);

CREATE TABLE deletions (
    constituent_id bigint PRIMARY KEY REFERENCES constituents(id),
    deleted_at timestamptz NOT NULL DEFAULT clock_timestamp()
);

CREATE INDEX constituents_current_idx ON constituents
    (index_code, isin, effective_date, load_id DESC, source_row DESC);
CREATE INDEX constituents_effective_date_idx ON constituents(effective_date);

-- Application bugs cannot mutate history, including through TRUNCATE.
-- An administrator can still disable triggers: this is not a tamper-proof ledger.
CREATE FUNCTION reject_history_mutation() RETURNS trigger LANGUAGE plpgsql AS $$
BEGIN
    RAISE EXCEPTION 'History is append-only';
END;
$$;
CREATE TRIGGER loads_immutable BEFORE UPDATE OR DELETE OR TRUNCATE ON loads
    FOR EACH STATEMENT EXECUTE FUNCTION reject_history_mutation();
CREATE TRIGGER constituents_immutable BEFORE UPDATE OR DELETE OR TRUNCATE ON constituents
    FOR EACH STATEMENT EXECUTE FUNCTION reject_history_mutation();
CREATE TRIGGER deletions_immutable BEFORE UPDATE OR DELETE OR TRUNCATE ON deletions
    FOR EACH STATEMENT EXECUTE FUNCTION reject_history_mutation();
COMMIT;

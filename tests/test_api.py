import csv
import hashlib
import io
import os
import uuid
from concurrent.futures import ThreadPoolExecutor
from pathlib import Path

import psycopg
import pytest
from fastapi.testclient import TestClient
from psycopg import sql

from app import main

pytestmark = pytest.mark.integration
HEADER = "index_code,isin,ticker,name,weight,shares,effective_date\n"
ROW = 'DEMO,US1234567890,TEST,"Example, Inc",15.8342,37070403,2026-01-02\n'


@pytest.fixture
def db(monkeypatch):
    dsn = os.getenv("TEST_DATABASE_URL")
    if not dsn:
        pytest.skip("Set TEST_DATABASE_URL to run real PostgreSQL integration tests")
    schema = "test_" + uuid.uuid4().hex
    with psycopg.connect(dsn, autocommit=True) as admin:
        admin.execute(sql.SQL("CREATE SCHEMA {}").format(sql.Identifier(schema)))

    def connection():
        return psycopg.connect(dsn, options=f"-c search_path={schema} -c lock_timeout=10000")

    try:
        with connection() as conn:
            conn.execute(Path(main.__file__).with_name("schema.sql").read_text())
        monkeypatch.setattr(main, "connect", connection)
        yield connection
    finally:
        with psycopg.connect(dsn, autocommit=True) as admin:
            admin.execute(sql.SQL("DROP SCHEMA {} CASCADE").format(sql.Identifier(schema)))


@pytest.fixture
def client(db):
    with TestClient(main.app) as client:
        yield client


def upload(client, text=HEADER + ROW, **kwargs):
    return client.post(
        "/uploads", files={"file": ("synthetic.csv", text.encode(), "text/csv")}, **kwargs
    )


def export(client, **kwargs):
    return client.get(
        "/export",
        params={
            "start_date": "2026-01-02",
            "end_date": "2026-01-02",
            **kwargs,
        },
    )


def test_repeat_upload_delete_and_reintroduce(client, db):
    first = upload(client)
    second = upload(client)
    assert first.status_code == second.status_code == 201
    assert first.json()["sha256"] == hashlib.sha256((HEADER + ROW).encode()).hexdigest()
    assert first.json()["load_id"] != second.json()["load_id"]
    current = export(client).json()
    assert len(current) == 1
    assert current[0]["load_id"] == second.json()["load_id"]
    row_id = current[0]["id"]
    assert client.delete(f"/rows/{row_id}").status_code == 204
    assert client.delete(f"/rows/{row_id}").status_code == 204
    assert export(client).json() == []  # Older versions must not resurrect.
    with db() as conn:
        assert conn.execute("SELECT count(*) FROM constituents").fetchone()[0] == 2
        assert conn.execute("SELECT count(*) FROM deletions").fetchone()[0] == 1
    assert upload(client).status_code == 201
    assert len(export(client).json()) == 1


def test_last_duplicate_wins_and_deleting_old_row_leaves_current(client, db):
    assert upload(client, HEADER + ROW + ROW.replace("15.8342", "20.0001")).status_code == 201
    result = export(client).json()[0]
    assert result["weight"] == "20.0001"
    assert result["source_row"] == 3
    with db() as conn:
        old_id = conn.execute("SELECT min(id) FROM constituents").fetchone()[0]
    assert client.delete(f"/rows/{old_id}").status_code == 204
    assert export(client).json()[0]["id"] == result["id"]


def test_csv_json_and_inclusive_date_range(client):
    assert upload(client, HEADER + ROW + ROW.replace("2026-01-02", "2026-04-01")).status_code == 201
    result = export(client)
    assert result.status_code == 200
    assert result.json()[0]["shares"] == "37070403"
    csv_response = export(client, format="csv")
    records = list(csv.DictReader(io.StringIO(csv_response.text)))
    assert len(records) == 1
    assert records[0]["name"] == "Example, Inc"
    assert records[0]["weight"] == result.json()[0]["weight"]
    assert len(export(client, end_date="2026-04-01").json()) == 2
    assert export(client, start_date="2027-01-01", end_date="2027-01-01").json() == []


def test_late_invalid_record_leaves_database_unchanged(client, db):
    response = upload(client, HEADER + ROW * 1001 + ROW.replace("15.8342", "bad"))
    assert response.status_code == 422
    with db() as conn:
        assert conn.execute("SELECT count(*) FROM loads").fetchone()[0] == 0
        assert conn.execute("SELECT count(*) FROM constituents").fetchone()[0] == 0


def test_batches_keep_every_version(client, db):
    assert upload(client, HEADER + ROW * 1002).json()["row_count"] == 1002
    with db() as conn:
        assert conn.execute("SELECT count(*) FROM constituents").fetchone()[0] == 1002
    assert export(client).json()[0]["source_row"] == 1003


def test_database_failure_rolls_back_entire_load(client, db):
    with db() as conn:
        conn.execute(
            "ALTER TABLE constituents ADD CONSTRAINT test_failure CHECK (source_row < 1003)"
        )
    assert upload(client, HEADER + ROW * 1002).status_code == 503
    with db() as conn:
        assert conn.execute("SELECT count(*) FROM loads").fetchone()[0] == 0
        assert conn.execute("SELECT count(*) FROM constituents").fetchone()[0] == 0


@pytest.mark.parametrize("table", ["loads", "constituents", "deletions"])
@pytest.mark.parametrize("operation", ["DELETE FROM {table}", "TRUNCATE {table} CASCADE"])
def test_database_rejects_mutation(client, db, table, operation):
    upload(client)
    with pytest.raises(psycopg.Error, match="append-only"):
        with db() as conn:
            conn.execute(operation.format(table=table))


def test_database_rejects_update(client, db):
    upload(client)
    with pytest.raises(psycopg.Error, match="append-only"):
        with db() as conn:
            conn.execute("UPDATE constituents SET weight = 1")


def test_parameter_and_file_validation(client, monkeypatch):
    assert export(client, start_date="bad").status_code == 422
    assert export(client, start_date="2026-02-01").status_code == 422
    assert export(client, format="xml").status_code == 422
    assert client.delete("/rows/0").status_code == 422
    assert client.delete("/rows/999999").status_code == 404
    assert client.post("/uploads", files={"file": ("bad.txt", b"bad")}).status_code == 415
    assert upload(client, params={"date_format": "guess"}).status_code == 422
    monkeypatch.setattr(main, "MAX_UPLOAD_BYTES", 10)
    assert upload(client).status_code == 413


def test_concurrent_uploads_have_deterministic_current_version(client, db):
    with ThreadPoolExecutor(max_workers=2) as pool:
        responses = list(pool.map(lambda _: upload(client), range(2)))
    assert all(response.status_code == 201 for response in responses)
    assert export(client).json()[0]["load_id"] == max(r.json()["load_id"] for r in responses)
    with db() as conn:
        assert conn.execute("SELECT count(*) FROM constituents").fetchone()[0] == 2


def test_original_sample_upload_export_delete_and_history(client, db):
    sample = Path(__file__).parents[1] / "data" / "index_constituents_sample.csv"
    original = sample.read_bytes()
    for _ in range(2):
        response = client.post("/uploads", files={"file": (sample.name, original, "text/csv")})
        assert response.status_code == 201
        assert response.json()["row_count"] == 54
        assert response.json()["sha256"] == hashlib.sha256(original).hexdigest()
    result = export(client, end_date="2026-07-01").json()
    assert len(result) == 54
    assert len(export(client).json()) == 18
    assert any(row["isin"] == "US478160104" for row in result)
    assert client.delete(f"/rows/{result[0]['id']}").status_code == 204
    assert len(export(client, end_date="2026-07-01").json()) == 53
    csv_result = export(client, end_date="2026-07-01", format="csv")
    assert len(list(csv.DictReader(io.StringIO(csv_result.text)))) == 53
    with db() as conn:
        assert conn.execute("SELECT count(*) FROM constituents").fetchone()[0] == 108
    assert sample.read_bytes() == original

# BITA constituent history API

A small FastAPI service that appends CSV records to PostgreSQL, hides rows through
deletion markers, and exports the current records effective within a date range.
Previously ingested rows are never updated or deleted. No `COPY` or native bulk
import is used.

## Run with Docker

Requires Docker with Compose. From this directory:

```sh
docker compose up --build -d db api
```

`constraints.txt` pins the dependency versions used for verification. The image
also includes the test dependencies to allow the same image to run the test suite.

Open http://localhost:8000/docs for interactive API documentation. The initial SQL
schema runs automatically when the database volume is first created. The volume
persists across restarts. `docker compose stop` stops the service while retaining data.
Changing `schema.sql` does not automatically migrate an existing database.

The supplied `bita` credentials are for this local assessment only. Both published
ports bind to localhost. This service has no authentication and is not configured
for public deployment.

## Run locally without Docker for the API

Requires Python 3.12+ and a running PostgreSQL 17 database. Create a database and
role first (for example `bita` with access to database `bita`). Set `DATABASE_URL`
to the actual connection string. The following commands use PowerShell:

```powershell
python -m venv .venv
.\.venv\Scripts\python.exe -m pip install -r requirements.txt
$env:DATABASE_URL = 'postgresql://bita:bita@localhost:5432/bita'
.\.venv\Scripts\python.exe -m app.init_db
.\.venv\Scripts\python.exe -m uvicorn app.main:app --host 127.0.0.1 --port 8000
```

Run `app.init_db` once on an empty database, not after every restart. If using the
Compose database, its schema already exists; omit that step. On macOS/Linux use
`.venv/bin/python` and `export DATABASE_URL='...'`.

## Use the API

The original recruiter CSV is included unchanged at
`data/index_constituents_sample.csv`. It has 54 records across two indices and
three effective dates: 2026-01-02, 2026-04-01, and 2026-07-01. Its raw dates use
ISO format, so the default upload settings work. Tests include both synthetic
records and the original file. Its SHA-256 fingerprint is recorded in `data/README.md`.

### Upload

```sh
curl -X POST "http://localhost:8000/uploads" -F "file=@data/index_constituents_sample.csv"
```

On Windows PowerShell use `curl.exe` instead of `curl`.

Success is HTTP 201 with `load_id`, `row_count`, and the SHA-256 digest of the
uploaded bytes. Uploading an identical file again intentionally creates a new load.
The hash is provenance, not a deduplication key. A retry after a lost response may
therefore create another complete load, but never destroys existing history.

Input is UTF-8 (optional BOM), comma-separated, with exactly these seven headers,
in any order:

```text
index_code,isin,ticker,name,weight,shares,effective_date
```

Dates default to `YYYY-MM-DD`. If inspection of the raw CSV confirms `DD-MM-YYYY`,
use `/uploads?date_format=dmy`. The service never guesses ambiguous dates from
Excel's display. API export query dates always use ISO dates.

Every field is required. Text is stored as supplied, including case and surrounding
spaces; empty/whitespace-only values and NUL bytes are rejected. Fields are limited
to 512 characters. Weight is a percentage from 0 through 100, inferred from the
supplied sample. Shares can be fractional. Both numeric fields use exact decimals,
with at most 12 decimal places and magnitude below `1e24`. These are documented
validation choices, not explicit assessment requirements. No checksum validation,
ticker/ISIN matching, weight-sum validation, or automatic data correction is done.

The supplied JPM rows contain `US478160104` (11 characters). This is retained as
an opaque supplied identifier rather than rejected or silently corrected. JNJ
has the distinct supplied value `US4781601046`.

Malformed/empty files return 422 with a record number where available; a wrong
filename extension returns 415; files above 20 MiB return 413. Blank records are
rejected. MIME type is not trusted as proof of CSV content.

### Export

```sh
curl "http://localhost:8000/export?start_date=2026-01-01&end_date=2026-12-31&format=json"
curl "http://localhost:8000/export?start_date=2026-01-01&end_date=2026-12-31&format=csv" -o export.csv
```

The date range is inclusive and filters `effective_date`, not upload time. An
invalid or reversed range or unsupported format returns 422. Empty output is an
empty JSON array or a CSV header. Export includes `id`, `load_id`, and `source_row`
before the seven input columns, so the caller can identify a row for deletion.
JSON represents decimals as strings to avoid floating-point precision loss.
The CSV export is an API result with metadata, not an input-file round trip.
Text is preserved exactly; import it as text when opening untrusted data in a
spreadsheet, whose formula interpretation differs from CSV's literal values.

### Delete

Use an `id` from the export:

```sh
curl -X DELETE "http://localhost:8000/rows/1"
```

Success and repeated deletion return 204. An unknown positive ID returns 404;
non-positive IDs return 422. Deleting a historical version does not hide a newer
version of the same business key.

## History and current-row rules

There are three append-only tables:

| Table | Purpose |
|---|---|
| `loads` | One record per successful file, with filename, hash, row count, and ingestion time |
| `constituents` | Every input record, with its load and CSV record number |
| `deletions` | One marker per deleted version, with deletion time |

The business key is `(index_code, isin, effective_date)`. The largest `load_id`
wins for each key; within the same load, the last CSV record wins. All duplicates
are retained. A PostgreSQL transaction advisory lock serializes writers, ensuring
successful load IDs follow write-commit order even under concurrent requests.
Gaps in IDs after rollback are normal. CSV record numbers count logical records
including the header, not physical lines inside quoted multiline fields.

The export query **resolves the latest version first, then checks deletion**.
Otherwise deleting today's version could accidentally reveal yesterday's version.
A later upload introduces a new version and can make that key visible again.
Different effective dates remain distinct keys; the service does not fill forward
missing dates or infer complete index snapshots from partial uploads.

The timestamp on a load records when it was registered inside its transaction,
not an exact historical commit timestamp. The API serves current knowledge of
effective dates; it does not claim to provide a transaction-time "what did users
see at this exact instant" endpoint. Retained versions and deletion markers remain
available for audits and future history endpoints.

For example, inspect all versions and deletion markers directly:

```sql
SELECT c.*, l.filename, l.ingested_at, d.deleted_at
FROM constituents c
JOIN loads l ON l.id = c.load_id
LEFT JOIN deletions d ON d.constituent_id = c.id
WHERE c.index_code = 'BITA100'
ORDER BY c.effective_date, c.isin, c.load_id, c.source_row;
```

Database triggers reject `UPDATE`, `DELETE`, and `TRUNCATE` on all three tables.
An administrator can disable triggers; these guard against application errors,
not privileged tampering. Backups and separate least-privilege database roles
would be required for production operations.

## Implementation decisions and tradeoffs

- **FastAPI:** input/query validation, OpenAPI documentation, and explicit response
  models. Synchronous handlers run blocking file/database work outside the event
  loop. PostgreSQL errors return a generic 503 and are logged server-side.
- **Python CSV reader:** incremental parsing without a dataframe or loading the
  complete file into Python objects. The seekable upload is hashed, validated,
  then parsed for insertion. Extra sequential reads avoid holding the write lock
  during the initial validation pass.
- **Psycopg:** direct parameterized SQL keeps the history query visible and small.
  `executemany` sends ordinary INSERT statements in batches of 1,000. It is not
  PostgreSQL native bulk import. SQLAlchemy would also work but adds an ORM layer
  without much benefit for these three operations.
- **One transaction per file:** either all records and load metadata commit, or
  none do. Per-batch commits were rejected because they leave partially ingested
  files after failure. Failed attempts are logged by the request/error path;
  they are not stored as successful loads.
- **Separate deletion markers:** even deletion is an append, instead of changing
  an `is_deleted` flag on the original row. The marker's uniqueness makes repeated
  deletes idempotent; no destructive operation occurs on conflict.
- **Exact PostgreSQL NUMERIC:** preserves decimal values without binary float
  rounding. Original numeric spelling is not retained (for example scientific
  notation may render differently), but original CSV bytes remain untouched.
- **Indexes and query order:** a business-key/version index supports latest-row
  selection, an effective-date index supports range filtering, and the deletion
  primary key supports marker lookup. Filtering dates before resolving versions
  is safe because effective date is part of the business key.
- **Bounded export memory:** a server-side cursor reads in batches. Output uses a
  temporary spool with a 1 MiB memory threshold, then disk. Rendering finishes
  before HTTP headers are sent, allowing query failures to return a proper error
  and releasing the DB connection before a slow client downloads the file. The
  tradeoff is disk use and delayed time to first byte. Temporary output is closed
  after delivery; it is not a permanent copy of the input.
- **Simple connection lifecycle:** one connection per request, with a 5-second
  connection timeout, 10-second lock timeout, and 60-second statement timeout.
  The write lock limits parallel ingestion. A pool, per-key ordering strategy,
  background jobs, and pagination would be next steps for higher concurrency.

The 20 MiB upload check occurs after multipart parsing; it bounds accepted CSV
size, not transport-level buffering or total temporary disk use. A production
deployment should enforce request-body limits before multipart parsing. Export
disk use and database query work scale with the requested range and retained
history. This implementation is designed for a small local assessment, not an
unbounded public ingestion service.

## Tests

Verification completed with Python 3.12 and PostgreSQL 17.11: **38 tests passed,
none skipped**, including the original 54-record sample. Docker and GitHub Actions
configurations are supplied; this verification used local PostgreSQL.

Run the complete suite against PostgreSQL with Docker:

```sh
docker compose --profile test run --build --rm test
```

Or use a local PostgreSQL test database and PowerShell:

```powershell
.\.venv\Scripts\python.exe -m pip install -r requirements-dev.txt
$env:TEST_DATABASE_URL = 'postgresql://bita:bita@localhost:5432/bita_test'
.\.venv\Scripts\python.exe -m pytest -q
.\.venv\Scripts\python.exe -m ruff check .
```

Create `bita_test` first. The test role needs permission to create schemas.
Each integration test creates and drops its own randomly named schema. Existing
application tables are not modified. If `TEST_DATABASE_URL` is absent, only parser
tests run and database tests explicitly skip. CI sets it and runs the full suite.

Tests cover exact decimals, quoted CSV, BOM, original byte preservation, explicit
date formats, validation, repeat uploads, duplicates, deletion semantics, inclusive
date ranges, batching, database-trigger immutability, concurrent uploads, and
whole-file rollback after a database failure beyond the first insertion batch.

## References

- [FastAPI file uploads](https://fastapi.tiangolo.com/tutorial/request-files/)
- [Psycopg transactions](https://www.psycopg.org/psycopg3/docs/basic/transactions.html)
- [Psycopg cursors](https://www.psycopg.org/psycopg3/docs/advanced/cursors.html)
- [Psycopg executemany/pipeline behavior](https://www.psycopg.org/psycopg3/docs/advanced/pipeline.html)

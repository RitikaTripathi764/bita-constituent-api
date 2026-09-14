import csv
import hashlib
import io
import json
import logging
import tempfile
from datetime import date
from itertools import islice
from typing import Annotated, Literal

import psycopg
from fastapi import FastAPI, File, HTTPException, Path, Query, Response, UploadFile
from fastapi.responses import JSONResponse, StreamingResponse
from pydantic import BaseModel
from starlette.background import BackgroundTask

from app.csv_data import COLUMNS, InvalidCSV, rows
from app.db import connect, lock_writes

app = FastAPI(title="BITA Constituent History", version="1.0.0")
logger = logging.getLogger(__name__)
MAX_UPLOAD_BYTES = 20 * 1024 * 1024
BATCH_SIZE = 1000
EXPORT_COLUMNS = ("id", "load_id", "source_row", *COLUMNS)

INSERT_ROW = """
INSERT INTO constituents
    (load_id, source_row, index_code, isin, ticker, name, weight, shares, effective_date)
VALUES (%s, %s, %s, %s, %s, %s, %s, %s, %s)
"""

EXPORT_SQL = """
WITH current_rows AS (
    SELECT DISTINCT ON (index_code, isin, effective_date) *
    FROM constituents
    WHERE effective_date BETWEEN %s AND %s
    ORDER BY index_code, isin, effective_date, load_id DESC, source_row DESC
)
SELECT c.id, c.load_id, c.source_row, c.index_code, c.isin, c.ticker, c.name,
       c.weight, c.shares, c.effective_date
FROM current_rows c
WHERE NOT EXISTS (SELECT 1 FROM deletions d WHERE d.constituent_id = c.id)
ORDER BY c.effective_date, c.index_code, c.isin
"""


class UploadResult(BaseModel):
    load_id: int
    row_count: int
    sha256: str


class ExportRow(BaseModel):
    id: int
    load_id: int
    source_row: int
    index_code: str
    isin: str
    ticker: str
    name: str
    weight: str  # Exact decimal strings in JSON, without floating-point rounding.
    shares: str
    effective_date: date


@app.exception_handler(psycopg.Error)
async def database_error(request, exc):
    logger.exception("Database request failed", exc_info=exc)
    return JSONResponse(
        status_code=503, content={"detail": "Database unavailable or busy; retry later"}
    )


@app.post("/uploads", status_code=201, response_model=UploadResult)
def upload(
    file: Annotated[UploadFile, File()],
    date_format: Literal["iso", "dmy"] = "iso",
):
    """Append an entire CSV atomically. Repeated files create new loads."""
    if not file.filename or not file.filename.lower().endswith(".csv"):
        raise HTTPException(415, "A .csv filename is required")
    digest = hashlib.sha256()
    size = 0
    file.file.seek(0)
    while chunk := file.file.read(65536):
        size += len(chunk)
        if size > MAX_UPLOAD_BYTES:
            raise HTTPException(413, "CSV exceeds the 20 MiB limit")
        digest.update(chunk)
    try:
        # Validate before holding the write lock. The temporary upload is seekable.
        count = sum(1 for _ in rows(file.file, date_format))
        with connect() as conn:
            lock_writes(conn)
            load_id = conn.execute(
                "INSERT INTO loads (filename, sha256, row_count) VALUES (%s, %s, %s) RETURNING id",
                (file.filename[:512], digest.hexdigest(), count),
            ).fetchone()[0]
            data = iter(rows(file.file, date_format))
            with conn.cursor() as cursor:
                while batch := list(islice(data, BATCH_SIZE)):
                    cursor.executemany(INSERT_ROW, [(load_id, *row) for row in batch])
        return UploadResult(load_id=load_id, row_count=count, sha256=digest.hexdigest())
    except InvalidCSV as exc:
        raise HTTPException(422, str(exc)) from exc


@app.delete("/rows/{row_id}", status_code=204, response_class=Response)
def delete_row(row_id: Annotated[int, Path(gt=0)]):
    """Append a deletion marker. Repeating this request is harmless."""
    with connect() as conn:
        lock_writes(conn)
        if conn.execute("SELECT 1 FROM constituents WHERE id = %s", (row_id,)).fetchone() is None:
            raise HTTPException(404, "Row not found")
        conn.execute(
            "INSERT INTO deletions (constituent_id) VALUES (%s) ON CONFLICT DO NOTHING",
            (row_id,),
        )
    return Response(status_code=204)


def file_chunks(output):
    try:
        while chunk := output.read(65536):
            yield chunk
    finally:
        output.close()


@app.get(
    "/export",
    response_model=list[ExportRow],
    responses={200: {"content": {"text/csv": {}}}},
)
def export(
    start_date: date,
    end_date: date,
    format: Annotated[Literal["json", "csv"], Query()] = "json",
):
    """Export latest versions effective within the inclusive range."""
    if start_date > end_date:
        raise HTTPException(422, "start_date must be on or before end_date")
    output = tempfile.SpooledTemporaryFile(max_size=1024 * 1024, mode="w+b")
    try:
        # Render before sending HTTP headers: DB failures can still return a 503.
        # The cursor bounds application memory; large output spills to local disk.
        with connect() as conn, conn.cursor(name="export_rows") as cursor:
            cursor.itersize = BATCH_SIZE
            cursor.execute(EXPORT_SQL, (start_date, end_date))
            if format == "json":
                output.write(b"[")
                first = True
                for record in cursor:
                    if not first:
                        output.write(b",")
                    output.write(
                        json.dumps(dict(zip(EXPORT_COLUMNS, record)), default=str).encode("utf-8")
                    )
                    first = False
                output.write(b"]")
            else:
                buffer = io.StringIO(newline="")
                writer = csv.writer(buffer)
                for record in (EXPORT_COLUMNS,):
                    writer.writerow(record)
                output.write(buffer.getvalue().encode("utf-8"))
                for record in cursor:
                    buffer.seek(0)
                    buffer.truncate(0)
                    writer.writerow(record)
                    output.write(buffer.getvalue().encode("utf-8"))
        output.seek(0)
    except BaseException:
        output.close()
        raise
    return StreamingResponse(
        file_chunks(output),
        media_type="application/json" if format == "json" else "text/csv",
        headers={"Content-Disposition": f'attachment; filename="constituents.{format}"'},
        background=BackgroundTask(output.close),
    )

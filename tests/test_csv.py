import hashlib
import io
from datetime import date
from decimal import Decimal
from pathlib import Path

import pytest

from app.csv_data import COLUMNS, InvalidCSV, rows


def test_original_sample_is_unchanged_and_all_records_parse():
    sample = Path(__file__).parents[1] / "data" / "index_constituents_sample.csv"
    original = sample.read_bytes()
    assert hashlib.sha256(original).hexdigest() == (
        "c88e8b46b3f30378c1db1bde694619d6507ece6ab8175cc4d202ea3ef3069aa0"
    )
    with sample.open("rb") as source:
        records = list(rows(source))
    assert len(records) == 54
    assert {row[-1] for row in records} == {date(2026, 1, 2), date(2026, 4, 1), date(2026, 7, 1)}
    assert any(row[2] == "US478160104" for row in records)
    assert sample.read_bytes() == original


HEADER = ",".join(COLUMNS) + "\n"
RECORD = 'DEMO,US1234567890,TEST,"Example, Inc",15.8342,37070403,2026-01-02\n'


def test_precision_quoted_name_and_original_bytes():
    original = ("\ufeff" + HEADER + RECORD).encode()
    source = io.BytesIO(original)
    result = list(rows(source))
    assert result[0][4] == "Example, Inc"
    assert result[0][5] == Decimal("15.8342")
    assert result[0][7] == date(2026, 1, 2)
    assert source.getvalue() == original
    assert not source.closed


@pytest.mark.parametrize(
    "body",
    [
        b"",
        HEADER.encode(),
        b"a,b\n1,2\n",
        (HEADER + "x,y,z\n").encode(),
        (HEADER + RECORD.replace("15.8342", "NaN")).encode(),
        (HEADER + RECORD.replace("15.8342", "101")).encode(),
        (HEADER + RECORD.replace("37070403", "Infinity")).encode(),
        (HEADER + RECORD.replace("37070403", "-1")).encode(),
        (HEADER + RECORD.replace("37070403", "1e25")).encode(),
        (HEADER + RECORD.replace("15.8342", "0.1234567890123")).encode(),
        (HEADER + RECORD.replace("2026-01-02", "2026-02-30")).encode(),
        (HEADER + RECORD.replace("2026-01-02", "02-01-2026")).encode(),
        (HEADER + RECORD.replace("TEST", "")).encode(),
        (HEADER + RECORD.replace("TEST", "A\x00B")).encode(),
        (HEADER + RECORD.replace("TEST", "x" * 513)).encode(),
        HEADER.encode() + b"\xff\n",
        (HEADER + '"unclosed').encode(),
        (HEADER + RECORD + "\n").encode(),
    ],
)
def test_rejects_invalid_csv(body):
    with pytest.raises(InvalidCSV):
        list(rows(io.BytesIO(body)))


def test_explicit_day_first_mode():
    source = io.BytesIO((HEADER + RECORD.replace("2026-01-02", "02-01-2026")).encode())
    assert list(rows(source, "dmy"))[0][-1] == date(2026, 1, 2)


def test_reordered_header():
    source = io.BytesIO(
        b"isin,index_code,ticker,name,weight,shares,effective_date\n"
        b"US1234567890,DEMO,TEST,Example,1,2,2026-01-02\n"
    )
    assert list(rows(source))[0][1:3] == ("DEMO", "US1234567890")

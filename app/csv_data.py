"""Strict, incremental CSV validation; input bytes are never modified."""

import csv
import io
import re
from datetime import date, datetime
from decimal import Decimal, InvalidOperation

COLUMNS = ("index_code", "isin", "ticker", "name", "weight", "shares", "effective_date")
csv.field_size_limit(65536)


class InvalidCSV(ValueError):
    pass


def parse_date(value, date_format):
    if date_format == "iso":
        if not re.fullmatch(r"\d{4}-\d{2}-\d{2}", value):
            raise ValueError("expected YYYY-MM-DD")
        return date.fromisoformat(value)
    if not re.fullmatch(r"\d{2}-\d{2}-\d{4}", value):
        raise ValueError("expected DD-MM-YYYY")
    return datetime.strptime(value, "%d-%m-%Y").date()


def decimal_value(value):
    result = Decimal(value)
    # Explicit bounds prevent PostgreSQL overflow and extreme numeric payloads.
    if not result.is_finite() or result < 0 or result >= Decimal("1e24"):
        raise ValueError("expected a finite non-negative number below 1e24")
    if result.as_tuple().exponent < -12:
        raise ValueError("at most 12 decimal places are supported")
    return result


def rows(binary_file, date_format="iso"):
    binary_file.seek(0)
    text_file = io.TextIOWrapper(binary_file, encoding="utf-8-sig", newline="")
    reader = csv.reader(text_file, strict=True)
    try:
        header = next(reader, None)
        if header is None or len(header) != len(COLUMNS) or set(header) != set(COLUMNS):
            raise InvalidCSV("Header must contain exactly: " + ", ".join(COLUMNS))
        positions = [header.index(column) for column in COLUMNS]
        count = 0
        for record_number, values in enumerate(reader, start=2):
            if len(values) != len(COLUMNS):
                raise InvalidCSV(f"CSV record {record_number}: expected seven fields")
            ordered = [values[position] for position in positions]
            if any(not v.strip() or "\x00" in v for v in ordered):
                raise InvalidCSV(f"CSV record {record_number}: empty or NUL-containing field")
            if any(len(v) > 512 for v in ordered):
                raise InvalidCSV(f"CSV record {record_number}: field exceeds 512 characters")
            try:
                weight = decimal_value(ordered[4])
                if weight > 100:
                    raise ValueError("weight must be between 0 and 100")
                shares = decimal_value(ordered[5])
                effective = parse_date(ordered[6], date_format)
            except (ValueError, InvalidOperation) as exc:
                raise InvalidCSV(f"CSV record {record_number}: {exc}") from exc
            count += 1
            yield (record_number, *ordered[:4], weight, shares, effective)
        if count == 0:
            raise InvalidCSV("CSV must contain at least one data record")
    except (UnicodeError, csv.Error) as exc:
        raise InvalidCSV(f"Invalid UTF-8 CSV near physical line {reader.line_num}") from exc
    finally:
        # UploadFile owns the underlying file; do not close it via this wrapper.
        text_file.detach()

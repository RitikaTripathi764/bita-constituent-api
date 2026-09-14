import os

import psycopg


def connect():
    return psycopg.connect(
        os.environ.get("DATABASE_URL", "postgresql://bita:bita@localhost:5432/bita"),
        connect_timeout=5,
        options="-c statement_timeout=60000 -c lock_timeout=10000",
    )


# Serializes writes so load ordering also follows commit ordering.
# This deliberately trades parallel ingestion throughput for simple semantics.
WRITE_LOCK = 724180923


def lock_writes(conn):
    conn.execute("SELECT pg_advisory_xact_lock(%s)", (WRITE_LOCK,))

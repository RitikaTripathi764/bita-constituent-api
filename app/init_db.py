from pathlib import Path

from app.db import connect


def main():
    with connect() as conn:
        conn.execute(Path(__file__).with_name("schema.sql").read_text())
    print("Database schema created.")


if __name__ == "__main__":
    main()

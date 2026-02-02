"""
Utility to clear all data in the active_learning DB tables for testing.
Keeps the schema; truncates labels, tiles, sessions (in dependency order).
"""
import sys
from sqlalchemy import create_engine, text
from sqlalchemy.exc import OperationalError

DB_URL = "postgresql://user:password@localhost:5432/active_learning"


def reset_db(confirm=True):
    engine = create_engine(DB_URL)
    if confirm:
        reply = input("Clear all data in labels, tiles, sessions? [y/N]: ").strip().lower()
        if reply != "y":
            print("Aborted.")
            return
    try:
        with engine.connect() as conn:
            conn.execute(text("TRUNCATE TABLE labels, tiles, sessions RESTART IDENTITY CASCADE"))
            conn.commit()
    except OperationalError as e:
        if "Connection refused" in str(e) or "could not connect" in str(e).lower():
            print("Cannot connect to PostgreSQL. Is the DB running?")
            print("  Start the container: docker start geospatial_db")
            print("  Or from labeler_db: docker-compose up -d")
        raise
    print("Done. labels, tiles, and sessions are empty.")


if __name__ == "__main__":
    reset_db(confirm="--yes" not in sys.argv and "-y" not in sys.argv)

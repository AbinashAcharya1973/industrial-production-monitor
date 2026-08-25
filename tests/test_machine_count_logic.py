import os
import sqlite3
import tempfile

from main import DatabaseManager


def build_fields(count, *, panel_id="1", zone="1", conveyor_name="Line 1"):
    return {
        "panel_id": panel_id,
        "year": 2025,
        "month": 8,
        "day": 18,
        "hour": 9,
        "minute": 30,
        "second": 0,
        "zone": zone,
        "conveyor_name": conveyor_name,
        "count": count,
    }


def test_same_count_is_ignored():
    with tempfile.TemporaryDirectory() as tmpdir:
        DatabaseManager.DB_FILE = os.path.join(tmpdir, "production.db")
        db = DatabaseManager()

        first_id = db.insert(build_fields(10))
        assert first_id > 0

        ignored = db.insert(build_fields(10))
        assert ignored == -1

        total_rows = db._conn.execute("SELECT COUNT(*) FROM production").fetchone()[0]
        assert total_rows == 1


def test_increment_adds_only_delta():
    with tempfile.TemporaryDirectory() as tmpdir:
        DatabaseManager.DB_FILE = os.path.join(tmpdir, "production.db")
        db = DatabaseManager()

        db.insert(build_fields(10))
        second_id = db.insert(build_fields(15))

        saved_count = db._conn.execute(
            "SELECT count FROM production WHERE ID = ?", (second_id,)
        ).fetchone()[0]
        assert saved_count == 5


def test_lower_count_triggers_reset_and_skips_negative_delta():
    with tempfile.TemporaryDirectory() as tmpdir:
        DatabaseManager.DB_FILE = os.path.join(tmpdir, "production.db")
        db = DatabaseManager()

        db.insert(build_fields(10))
        reset_result = db.insert(build_fields(8))
        assert reset_result == -1

        third_id = db.insert(build_fields(12))
        saved_count = db._conn.execute(
            "SELECT count FROM production WHERE ID = ?", (third_id,)
        ).fetchone()[0]
        assert saved_count == 4

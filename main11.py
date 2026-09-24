#!/usr/bin/env python3
"""
Industrial Production Monitor
PyQt5 serial data receiving application for conveyor line production monitoring.

Data pipeline (strict one-way flow):
  SERIAL RX  →  parse fields  →  INSERT into SQLite  →  SELECT from SQLite  →  render UI table
"""

import sys
import os
import csv
import sqlite3
import threading
import time
from datetime import datetime, date, timedelta

import serial
import serial.tools.list_ports

from PyQt5.QtWidgets import (
    QApplication, QMainWindow, QWidget, QVBoxLayout, QHBoxLayout,
    QLabel, QPushButton, QComboBox, QTableWidget, QTableWidgetItem,
    QHeaderView, QFrame, QTabWidget, QTextEdit,
    QFileDialog, QMessageBox, QAbstractItemView, QScrollArea,
    QFormLayout, QSpinBox,
)
from PyQt5.QtCore import Qt, QTimer, QThread, pyqtSignal, QObject
from PyQt5.QtGui import QColor


# ═══════════════════════════════════════════════════════════════
#  STEP 1 – Parse incoming serial line into named fields
# ═══════════════════════════════════════════════════════════════
def clean_line(raw_line: str) -> str:
    """
    Strip non-printable and non-ASCII characters from a raw serial line,
    then locate the first 10-field CSV segment within what remains.

    Handles two common corruption patterns:
      - Non-UTF-8 garbage bytes decoded as replacement chars (□□□1,21,5,...)
      - Latin-1 / binary prefix bytes decoded as letters (ÿÿ1,21,5,...)
    """
    # 1. Keep only printable ASCII
    cleaned = "".join(ch for ch in raw_line if 32 <= ord(ch) <= 126)

    # 2. Find the first digit in the string — real data always starts with a digit
    first_digit = next((i for i, ch in enumerate(cleaned) if ch.isdigit()), None)
    if first_digit is None:
        return ""
    cleaned = cleaned[first_digit:]

    # 3. Return the cleaned line (fields will be validated in parse_packet)
    return cleaned.strip()


def parse_packet(raw_line: str) -> dict | None:
    """
    Parse a comma-separated serial packet into a field dict.

    The conveyor "M/C No.XX/YYY" field is the KEY/anchor of the packet:
    every field the app needs sits at a fixed offset around it.

        ..., PanelID, Year, Month, Day, Hour, Minute, Second, Zone, M/C No.XX/YYY, Count

    The LoRa link often prepends junk fields from a previous line, e.g.
        999,453,1,26,9,23,20,35,18,1,M/C No.01/850,453      (2 junk fields)
    Counting fields from the start rejects these lines. Anchoring on the
    M/C No. field instead makes them parse, while clean lines like
        1,26,9,23,20,35,4,2,M/C No.08/450,10087
    parse exactly as before.

    Returns a dict with all fields, or None if the line is invalid.
    """
    parts = [p.strip() for p in raw_line.split(",")]
    if not parts:
        return None

    # ── Locate the conveyor (M/C No.) field — the packet's key ──
    mc_idx = None
    for i, p in enumerate(parts):
        if "M/C" in p.upper():
            mc_idx = i
            break
    if mc_idx is None:
        # Fallback for non-M/C conveyor names (e.g. "Conveyor Line 1"):
        # use the LAST field containing a letter. Junk prefixes (e.g.
        # "n6626") always appear BEFORE the real packet, so the last
        # lettered field is still the conveyor name.
        for i in range(len(parts) - 1, -1, -1):
            if any(ch.isalpha() for ch in parts[i]):
                mc_idx = i
                break
    # Need 8 fields before the key (panel..zone) and the count after it
    if mc_idx is None or mc_idx < 8 or mc_idx + 1 >= len(parts):
        return None

    try:
        panel_id = int(parts[mc_idx - 8])
        year     = int(parts[mc_idx - 7])
        if year < 100:            # HMI sends 2-digit year (e.g. 26 → 2026)
            year += 2000          # Quick filters compare against 4-digit years
        month  = int(parts[mc_idx - 6])
        day    = int(parts[mc_idx - 5])
        hour   = int(parts[mc_idx - 4])
        minute = int(parts[mc_idx - 3])
        second = int(parts[mc_idx - 2])
        zone   = str(int(parts[mc_idx - 1]))   # stored as TEXT in DB
        count  = int(parts[mc_idx + 1])
    except (ValueError, IndexError):
        return None

    # Sanity-check the anchored fields so a wrong anchor can't store garbage
    if not (2000 <= year <= 2099 and 1 <= month <= 12 and 1 <= day <= 31
            and 0 <= hour <= 23 and 0 <= minute <= 59 and 0 <= second <= 59):
        return None

    # Normalize conveyor name: strip stray non-printable/control chars
    # that survive clean_line(), collapse internal whitespace, and trim.
    # Without this, two packets for the same physical machine can produce
    # slightly different strings (trailing \x00, double space, etc.) and
    # GROUP BY treats them as different machines -> duplicate UI rows.
    raw_conveyor_name = parts[mc_idx]
    conveyor_name = "".join(
        ch for ch in raw_conveyor_name if 32 <= ord(ch) <= 126
    )
    conveyor_name = " ".join(conveyor_name.split())  # collapse whitespace

    if not conveyor_name:
        return None

    return {
        "panel_id":      str(panel_id),
        "year":          year,
        "month":         month,
        "day":           day,
        "hour":          hour,
        "minute":        minute,
        "second":        second,
        "zone":          zone,
        "conveyor_name": conveyor_name,
        "count":         count,
    }


# ═══════════════════════════════════════════════════════════════
#  STEP 2 – INSERT into SQLite  +  all SELECT queries
# ═══════════════════════════════════════════════════════════════
class DatabaseManager:
    """
    Single source of truth for all database operations.

    Tables:
        products (
            slno    VARCHAR PRIMARY KEY,
            partno  VARCHAR
        )

        production (
            ID          INTEGER PRIMARY KEY AUTOINCREMENT,
            panel_id    TEXT,
            year        INTEGER,
            month       INTEGER,
            day         INTEGER,
            hour        INTEGER,
            minute      INTEGER,
            second      INTEGER,
            zone        TEXT,
            conveyor    TEXT,
            count       INTEGER,
            productslno VARCHAR   -- FK → products.slno
        )
    """

    DB_FILE = "production.db"

    def __init__(self):
        self._lock = threading.Lock()
        self._conn = sqlite3.connect(self.DB_FILE, check_same_thread=False)
        self._conn.row_factory = sqlite3.Row
        self._conn.execute("PRAGMA foreign_keys = ON")
        self._last_count_by_machine = {}
        self._create_tables()
        self._load_last_counts()

    def _machine_key(self, fields: dict) -> tuple[str, str, str]:
        return (
            str(fields.get("panel_id", "")),
            str(fields.get("zone", "")),
            str(fields.get("conveyor_name", "")).strip(),
        )

    # ── DDL ───────────────────────────────────────────────────
    def _create_tables(self):
        with self._lock:
            # products table
            self._conn.execute("""
                CREATE TABLE IF NOT EXISTS products (
                    slno   VARCHAR PRIMARY KEY,
                    partno VARCHAR
                )
            """)
            # production table
            self._conn.execute("""
                CREATE TABLE IF NOT EXISTS production (
                    ID          INTEGER PRIMARY KEY AUTOINCREMENT,
                    panel_id    TEXT,
                    year        INTEGER,
                    month       INTEGER,
                    day         INTEGER,
                    hour        INTEGER,
                    minute      INTEGER,
                    second      INTEGER,
                    zone        TEXT,
                    conveyor    TEXT,
                    count       INTEGER,
                    productslno VARCHAR,
                    target      INTEGER DEFAULT 0,
                    FOREIGN KEY (productslno) REFERENCES products(slno)
                )
            """)
            # machine_list table
            self._conn.execute("""
                CREATE TABLE IF NOT EXISTS machine_list (
                    mslno         VARCHAR PRIMARY KEY,
                    conveyorname VARCHAR
                )
            """)
            # production_plan table
            self._conn.execute("""
                CREATE TABLE IF NOT EXISTS production_plan (
                    mslno       INTEGER PRIMARY KEY,
                    conveyorname VARCHAR,
                    productslno VARCHAR,
                    target      INTEGER
                )
            """)
            # machine_counter_state table: stores the latest count observed per machine
            self._conn.execute("""
                CREATE TABLE IF NOT EXISTS machine_counter_state (
                    panel_id TEXT NOT NULL,
                    zone TEXT NOT NULL,
                    conveyor TEXT NOT NULL,
                    last_count INTEGER NOT NULL DEFAULT 0,
                    PRIMARY KEY (panel_id, zone, conveyor)
                )
            """)
            # Migration: add productslno and target columns to existing DBs that lack them
            existing = {
                row[1] for row in
                self._conn.execute("PRAGMA table_info(production)").fetchall()
            }
            if "productslno" not in existing:
                self._conn.execute(
                    "ALTER TABLE production ADD COLUMN productslno VARCHAR"
                )
            if "target" not in existing:
                self._conn.execute(
                    "ALTER TABLE production ADD COLUMN target INTEGER DEFAULT 0"
                )

            # Migration: normalize 2-digit years stored by older versions
            # (HMI sends year as 26, not 2026). Quick filters ("Today",
            # "Last 7/30 Days") compare against 4-digit years, so rows with
            # 2-digit years never match and disappear from those views.
            self._conn.execute(
                "UPDATE production SET year = year + 2000 WHERE year < 100"
            )

            # Migration: normalize existing conveyor names that may contain
            # stray non-printable characters or inconsistent whitespace
            # from before this cleaning was added. Without this, rows for
            # the same physical machine end up as separate GROUP BY keys
            # and appear as duplicate rows in the Production Table / 24h View.
            rows = self._conn.execute(
                "SELECT DISTINCT conveyor FROM production"
            ).fetchall()
            for (raw_name,) in rows:
                if raw_name is None:
                    continue
                cleaned = "".join(ch for ch in raw_name if 32 <= ord(ch) <= 126)
                cleaned = " ".join(cleaned.split())
                if cleaned != raw_name:
                    self._conn.execute(
                        "UPDATE production SET conveyor = ? WHERE conveyor = ?",
                        (cleaned, raw_name),
                    )

            # Migration: deduplicate the products table.
            # If products was created by an earlier version of this app
            # WITHOUT the PRIMARY KEY constraint (or rows were inserted
            # directly via a DB tool), the same slno can appear multiple
            # times — one with partno empty, one with partno filled in.
            # A LEFT JOIN against a products table with duplicate slno
            # values multiplies the joined row once per match, which is
            # exactly the "same row repeated N times, only Part No differs"
            # symptom seen in the UI. Rebuild products with a real PK and
            # keep the row with the most useful (longest/non-empty) partno.
            dup_check = self._conn.execute("""
                SELECT slno, COUNT(*) AS cnt
                FROM products
                GROUP BY slno
                HAVING cnt > 1
            """).fetchall()
            if dup_check:
                self._conn.execute("""
                    CREATE TABLE products_dedup AS
                    SELECT slno, partno FROM (
                        SELECT slno, partno,
                               ROW_NUMBER() OVER (
                                   PARTITION BY slno
                                   ORDER BY LENGTH(COALESCE(partno,'')) DESC, rowid DESC
                               ) AS rn
                        FROM products
                    )
                    WHERE rn = 1
                """)
                self._conn.execute("DROP TABLE products")
                self._conn.execute(
                    "CREATE TABLE products (slno VARCHAR PRIMARY KEY, partno VARCHAR)"
                )
                self._conn.execute(
                    "INSERT INTO products (slno, partno) "
                    "SELECT slno, partno FROM products_dedup"
                )
                self._conn.execute("DROP TABLE products_dedup")

            self._conn.commit()

    # ── Products table helpers ────────────────────────────────
    def upsert_product(self, slno: str, partno: str = ""):
        """
        Insert a product row if it doesn't exist yet.
        Called automatically before every production INSERT so the FK is satisfied.
        """
        with self._lock:
            self._conn.execute("""
                INSERT OR IGNORE INTO products (slno, partno) VALUES (?, ?)
            """, (slno, partno))
            self._conn.commit()

    def get_all_products(self) -> list[sqlite3.Row]:
        """SELECT * FROM products ORDER BY slno"""
        with self._lock:
            return self._conn.execute(
                "SELECT slno, partno FROM products ORDER BY slno"
            ).fetchall()

    def get_all_machines(self) -> list[sqlite3.Row]:
        """SELECT * FROM machine_list ORDER BY mslno"""
        with self._lock:
            return self._conn.execute(
                "SELECT mslno, conveyorname FROM machine_list ORDER BY mslno"
            ).fetchall()

    def upsert_machine(self, mslno: str, conveyorname: str = ""):
        """
        Insert or update a machine row in machine_list.
        """
        with self._lock:
            self._conn.execute("""
                INSERT OR REPLACE INTO machine_list (mslno, conveyorname)
                VALUES (?, ?)
            """, (mslno, conveyorname))
            self._conn.commit()

    def save_production_plan(self, plan_data: dict):
        """
        Save production plan data. Clears existing plan and saves new one.
        plan_data format: {
            "mslno": {
                "conveyorname": "name",
                "product": "productslno",
                "target": target_value
            }
        }
        """
        with self._lock:
            # Clear existing plan
            self._conn.execute("DELETE FROM production_plan")
            # Insert new plan
            for mslno, data in plan_data.items():
                self._conn.execute("""
                    INSERT INTO production_plan (mslno, conveyorname, productslno, target)
                    VALUES (?, ?, ?, ?)
                """, (mslno, data["conveyorname"], data["product"], data["target"]))
            self._conn.commit()

    def get_production_plan(self) -> list[sqlite3.Row]:
        """SELECT * FROM production_plan ORDER BY mslno"""
        with self._lock:
            return self._conn.execute(
                "SELECT mslno, conveyorname, productslno, target FROM production_plan ORDER BY mslno"
            ).fetchall()

    def export_production_plan_csv(self, filepath: str) -> int:
        """
        Export the CURRENT (pre-update) production_plan table to a CSV file.
        Returns the number of rows exported (0 if the plan was empty).
        Joins against products to also include partno for readability.
        """
        with self._lock:
            rows = self._conn.execute("""
                SELECT
                    pp.mslno, pp.conveyorname, pp.productslno,
                    pr.partno, pp.target
                FROM production_plan pp
                LEFT JOIN products pr ON pr.slno = pp.productslno
                ORDER BY pp.mslno
            """).fetchall()
        with open(filepath, "w", newline="") as f:
            writer = csv.writer(f)
            writer.writerow(["mslno", "conveyorname", "productslno", "partno", "target"])
            for r in rows:
                writer.writerow(list(r))
        return len(rows)

    def update_product_partno(self, slno: str, partno: str):
        """UPDATE products SET partno = ? WHERE slno = ?"""
        with self._lock:
            self._conn.execute(
                "UPDATE products SET partno = ? WHERE slno = ?", (partno, slno)
            )
            self._conn.commit()

    # ── STEP 2a: INSERT ───────────────────────────────────────
    def _load_last_counts(self):
        """Refresh the in-memory cache from the persistent machine_counter_state table."""
        with self._lock:
            rows = self._conn.execute("""
                SELECT panel_id, zone, conveyor, last_count
                FROM machine_counter_state
            """).fetchall()
        self._last_count_by_machine = {
            (str(row["panel_id"]), str(row["zone"]), str(row["conveyor"])): int(row["last_count"])
            for row in rows
        }

    def _upsert_machine_counter_state(self, panel_id: str, zone: str, conveyor: str, last_count: int):
        """Persist the latest machine total for a given panel/zone/conveyor."""
        key = (str(panel_id), str(zone), str(conveyor))
        with self._lock:
            self._conn.execute("""
                INSERT INTO machine_counter_state (panel_id, zone, conveyor, last_count)
                VALUES (?, ?, ?, ?)
                ON CONFLICT(panel_id, zone, conveyor)
                DO UPDATE SET last_count = excluded.last_count
            """, (panel_id, zone, conveyor, int(last_count)))
            self._conn.commit()
        self._last_count_by_machine[key] = int(last_count)

    def insert(self, fields: dict) -> int:
        """
        INSERT one parsed-packet dict into the production table.

        Rules for machine counters:
          1. If the same count value is received again, ignore it.
          2. If the new count is greater than the previous one, insert only the delta.
          3. If the new count is lower than the previous one, treat it as a reset and
             update the stored baseline without inserting a row.
          4. If no previous count exists for the machine, insert the first value as baseline.
        """
        conveyor_name = fields.get("conveyor_name", "")
        machine_key = self._machine_key(fields)

        previous_count = self._last_count_by_machine.get(machine_key)
        if previous_count is None:
            with self._lock:
                row = self._conn.execute("""
                    SELECT last_count
                    FROM machine_counter_state
                    WHERE panel_id = ? AND zone = ? AND conveyor = ?
                """, (machine_key[0], machine_key[1], machine_key[2])).fetchone()
            if row is not None:
                previous_count = int(row[0])
                self._last_count_by_machine[machine_key] = previous_count

        current_count = int(fields["count"])
        if previous_count is not None:
            if current_count == previous_count:
                return -1
            if current_count < previous_count:
                self._upsert_machine_counter_state(
                    machine_key[0], machine_key[1], machine_key[2], current_count
                )
                return -1
            current_count = current_count - previous_count
            fields = dict(fields)
            fields["count"] = current_count

        # Fetch productslno and target from production_plan based on conveyor_name
        productslno = None
        target = 0
        with self._lock:
            row = self._conn.execute("""
                SELECT productslno, target FROM production_plan WHERE conveyorname = ?
            """, (conveyor_name,)).fetchone()
            if row:
                productslno = row[0]
                target = row[1] if row[1] is not None else 0

        # Ensure FK reference exists before inserting production row
        if productslno:
            self.upsert_product(productslno)

        with self._lock:
            cur = self._conn.execute("""
                INSERT INTO production
                    (panel_id, year, month, day, hour, minute, second,
                     zone, conveyor, count, productslno, target)
                VALUES
                    (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
            """, (fields["panel_id"], fields["year"], fields["month"],
                   fields["day"], fields["hour"], fields["minute"],
                   fields["second"], fields["zone"], conveyor_name,
                   fields["count"], productslno, target))
            self._conn.commit()

        state_total = int(fields["count"]) + int(previous_count or 0)
        self._upsert_machine_counter_state(
            machine_key[0], machine_key[1], machine_key[2], state_total
        )
        return cur.lastrowid

    # ── STEP 2b: SELECT for Production Table tab ──────────────
    def query_production_table(
        self,
        quick_filter: str = "all",
        panel_id:    str | None = None,
        zone:        str | None = None,
        conveyor:    str | None = None,
    ) -> list[sqlite3.Row]:
        """
        SELECT aggregated hourly counts grouped by
        (panel_id, year, month, day, zone, conveyor, productslno, target) — one row per unique combination per day.

        Each unique product and target combination gets its own row with hourly breakdowns.
        LEFT JOIN products to get partno from productslno.

        SQL produced:
            SELECT
                agg.panel_id, agg.year, agg.month, agg.day,
                agg.zone, agg.conveyor,
                agg.productslno,
                pr.partno,
                agg.target,
                agg.hour_0 … agg.hour_23,
                agg.day_total
            FROM (
                SELECT
                    panel_id, year, month, day, zone, conveyor,
                    productslno, target,
                    SUM(CASE WHEN hour=0  THEN count ELSE 0 END) AS hour_0,
                    ...
                    SUM(count) AS day_total
                FROM production [WHERE ...]
                GROUP BY panel_id, year, month, day, zone, conveyor, productslno, target
            ) agg
            LEFT JOIN products pr ON pr.slno = agg.productslno
            ORDER BY agg.year, agg.month, agg.day, agg.panel_id, agg.zone, agg.conveyor
        """
        hour_pivots = ",\n".join(
            f"    SUM(CASE WHEN hour={h} THEN count ELSE 0 END) AS hour_{h}"
            for h in range(24)
        )
        where, params = self._build_where(
            quick_filter, panel_id, zone, conveyor, alias=""
        )
        sql = f"""
            SELECT
                agg.panel_id, agg.year, agg.month, agg.day,
                agg.zone, agg.conveyor,
                agg.productslno,
                pr.partno,
                agg.target,
                { ", ".join(f"agg.hour_{h}" for h in range(24)) },
                agg.day_total
            FROM (
                SELECT
                    panel_id, year, month, day, zone, conveyor,
                    MAX(CASE WHEN productslno IS NOT NULL THEN productslno END) AS productslno,
                    MAX(target) AS target,
                {hour_pivots},
                    SUM(count) AS day_total
                FROM production
                {where}
                GROUP BY panel_id, year, month, day, zone, conveyor
            ) agg
            LEFT JOIN products pr ON pr.slno = agg.productslno
            ORDER BY agg.year, agg.month, agg.day,
                     agg.panel_id, agg.zone, agg.conveyor
        """
        with self._lock:
            return self._conn.execute(sql, params).fetchall()

    # ── STEP 2c: SELECT for 24-Hour View tab ─────────────────
    def query_24h_view(
        self,
        quick_filter: str = "all",
        panel_id:    str | None = None,
        zone:        str | None = None,
        conveyor:    str | None = None,
    ) -> list[sqlite3.Row]:
        """
        SELECT hourly totals collapsed across all dates, grouped by
        (panel_id, zone, conveyor, productslno, target) — one row per unique product/target combination per conveyor.

        LEFT JOIN products to get partno from productslno.
        """
        hour_pivots = ",\n".join(
            f"    SUM(CASE WHEN hour={h} THEN count ELSE 0 END) AS hour_{h}"
            for h in range(24)
        )
        where, params = self._build_where(
            quick_filter, panel_id, zone, conveyor, alias=""
        )
        sql = f"""
            SELECT
                agg.panel_id, agg.zone, agg.conveyor,
                agg.productslno,
                pr.partno,
                agg.target,
                { ", ".join(f"agg.hour_{h}" for h in range(24)) },
                agg.period_total
            FROM (
                SELECT
                    panel_id, zone, conveyor,
                    MAX(CASE WHEN productslno IS NOT NULL THEN productslno END) AS productslno,
                    MAX(target) AS target,
                {hour_pivots},
                    SUM(count) AS period_total
                FROM production
                {where}
                GROUP BY panel_id, zone, conveyor
            ) agg
            LEFT JOIN products pr ON pr.slno = agg.productslno
            ORDER BY agg.panel_id, agg.zone, agg.conveyor
        """
        with self._lock:
            return self._conn.execute(sql, params).fetchall()

    # ── STEP 2d: SELECT for filter dropdowns ─────────────────
    def query_distinct_values(self) -> dict:
        with self._lock:
            panels = [r[0] for r in self._conn.execute(
                "SELECT DISTINCT panel_id FROM production ORDER BY panel_id"
            ).fetchall()]
            zones = [r[0] for r in self._conn.execute(
                "SELECT DISTINCT zone FROM production ORDER BY zone"
            ).fetchall()]
            conveyors = [r[0] for r in self._conn.execute(
                "SELECT DISTINCT conveyor FROM production ORDER BY conveyor"
            ).fetchall()]
        return {"panels": panels, "zones": zones, "conveyors": conveyors}

    # ── STEP 2e: SELECT for stat cards ────────────────────────
    def query_stats(self) -> dict:
        with self._lock:
            row = self._conn.execute("""
                SELECT
                    COALESCE(SUM(count), 0)                                    AS total_count,
                    COUNT(*)                                                    AS total_rows,
                    COUNT(DISTINCT panel_id || '|' || zone || '|' || conveyor) AS line_count
                FROM production
            """).fetchone()
        return dict(row)

    # ── Utility ───────────────────────────────────────────────
    def _build_where(self, quick_filter, panel_id, zone, conveyor,
                     alias: str = "") -> tuple[str, list]:
        """Build the WHERE clause. Uses table alias if provided (e.g. 'p')."""
        prefix = f"{alias}." if alias else ""
        clauses, params = [], []

        today = date.today()
        if quick_filter == "today":
            clauses.append(
                f"({prefix}year = ? AND {prefix}month = ? AND {prefix}day = ?)"
            )
            params += [today.year, today.month, today.day]
        elif quick_filter == "7days":
            d = today - timedelta(days=6)
            clauses.append(
                f"({prefix}year*10000 + {prefix}month*100 + {prefix}day) >= ?"
            )
            params.append(d.year * 10000 + d.month * 100 + d.day)
        elif quick_filter == "30days":
            d = today - timedelta(days=29)
            clauses.append(
                f"({prefix}year*10000 + {prefix}month*100 + {prefix}day) >= ?"
            )
            params.append(d.year * 10000 + d.month * 100 + d.day)

        if panel_id and panel_id != "All":
            clauses.append(f"{prefix}panel_id = ?")
            params.append(panel_id)
        if zone and zone != "All":
            clauses.append(f"{prefix}zone = ?")
            params.append(zone)
        if conveyor and conveyor != "All":
            clauses.append(f"{prefix}conveyor = ?")
            params.append(conveyor)

        where = ("WHERE " + " AND ".join(clauses)) if clauses else ""
        return where, params

    def delete_all(self):
        with self._lock:
            self._conn.execute("DELETE FROM production")
            self._conn.execute("DELETE FROM products")
            # Also reset per-machine counter baselines: without this, idle
            # machines resend the same count after a Clear, it matches the
            # stale baseline, every packet is ignored, and the table stays
            # empty ("no data after Clear + reconnect" symptom).
            self._conn.execute("DELETE FROM machine_counter_state")
            self._conn.commit()
        self._last_count_by_machine = {}


    def export_csv(self, filepath: str):
        """Export production table joined with products to CSV."""
        with self._lock:
            rows = self._conn.execute("""
                SELECT
                    p.ID, p.panel_id, p.year, p.month, p.day,
                    p.hour, p.minute, p.second,
                    p.zone, p.conveyor, p.count,
                    p.productslno, pr.partno, p.target
                FROM production p
                LEFT JOIN products pr ON pr.slno = p.productslno
                ORDER BY p.ID
            """).fetchall()
        with open(filepath, "w", newline="") as f:
            writer = csv.writer(f)
            writer.writerow([
                "ID", "panel_id", "year", "month", "day",
                "hour", "minute", "second", "zone", "conveyor",
                "count", "productslno", "partno", "target"
            ])
            for r in rows:
                writer.writerow(list(r))

    def close(self):
        self._conn.close()


# ═══════════════════════════════════════════════════════════════
#  Serial worker (runs in a QThread, emits raw lines)
# ═══════════════════════════════════════════════════════════════
class SerialWorker(QObject):
    line_received  = pyqtSignal(str)   # one raw comma-separated line
    error_occurred = pyqtSignal(str)
    finished       = pyqtSignal()

    def __init__(self, port: str, baud: int):
        super().__init__()
        self.port      = port
        self.baud      = baud
        self._running  = True

    def run(self):
        try:
            ser = serial.Serial(
                port=self.port, baudrate=self.baud,
                parity=serial.PARITY_NONE, stopbits=serial.STOPBITS_ONE,
                bytesize=serial.EIGHTBITS, timeout=1,
            )
        except Exception as e:
            self.error_occurred.emit(str(e))
            self.finished.emit()
            return

        while self._running:
            try:
                if ser.in_waiting > 0:
                    raw = ser.readline()
                    try:
                        decoded = raw.decode("utf-8")
                    except UnicodeDecodeError:
                        # latin-1 never fails — every byte 0x00-0xFF is valid
                        decoded = raw.decode("latin-1")
                    # Handle literal \r\n strings from some firmware
                    if "\\r\\n" in decoded:
                        decoded = decoded.replace("\\r\\n", "\n")
                    decoded = decoded.rstrip("\r\n")
                    for line in decoded.split("\n"):
                        line = line.strip()
                        if line:
                            self.line_received.emit(line)
            except Exception as e:
                self.error_occurred.emit(str(e))
                break
            time.sleep(0.01)

        if ser.is_open:
            ser.close()
        self.finished.emit()

    def stop(self):
        self._running = False


# ═══════════════════════════════════════════════════════════════
#  Stat card widget
# ═══════════════════════════════════════════════════════════════
class StatCard(QFrame):
    def __init__(self, title: str, value: str = "0",
                 color: str = "#00ff88", parent=None):
        super().__init__(parent)
        self.setFrameShape(QFrame.Box)
        self.setObjectName("statCard")
        layout = QVBoxLayout(self)
        layout.setContentsMargins(16, 10, 16, 10)
        layout.setSpacing(4)

        lbl_title = QLabel(title)
        lbl_title.setObjectName("cardTitle")

        self._lbl_value = QLabel(value)
        self._lbl_value.setObjectName("cardValue")
        self._lbl_value.setStyleSheet(f"color: {color};")

        layout.addWidget(lbl_title)
        layout.addWidget(self._lbl_value)

    def set_value(self, val):
        self._lbl_value.setText(str(val))


# ═══════════════════════════════════════════════════════════════
#  Main window
# ═══════════════════════════════════════════════════════════════
class IndustrialMonitor(QMainWindow):

    _quick_filter = "all"

    def __init__(self):
        super().__init__()
        self.setWindowTitle("Industrial Production Monitor")
        self.resize(1440, 820)

        # Runtime state (counters only — no data cache; DB is the source of truth)
        self._dark          = True
        self._packets_rx    = 0
        self._errors        = 0
        self._start_time    = datetime.now()
        self._connected     = False
        self._serial_thread = None
        self._serial_worker = None

        # Database (opened once for the lifetime of the app)
        self._db = DatabaseManager()

        self._build_ui()
        self._apply_theme()
        self._refresh_ports()

        # Populate UI from any existing rows in the DB
        self._refresh_ui_from_db()

        # Uptime ticker
        self._uptime_timer = QTimer()
        self._uptime_timer.timeout.connect(self._tick_uptime)
        self._uptime_timer.start(1000)

        # Clock
        self._clock_timer = QTimer()
        self._clock_timer.timeout.connect(self._tick_clock)
        self._clock_timer.start(1000)
        self._tick_clock()

    # ──────────────────────────────────────────
    #  UI construction
    # ──────────────────────────────────────────
    def _build_ui(self):
        central = QWidget()
        self.setCentralWidget(central)
        root = QVBoxLayout(central)
        root.setContentsMargins(0, 0, 0, 0)
        root.setSpacing(0)
        root.addWidget(self._build_toolbar())
        root.addWidget(self._build_body())

    def _build_toolbar(self):
        bar = QWidget()
        bar.setObjectName("toolbar")
        bar.setFixedHeight(52)
        layout = QHBoxLayout(bar)
        layout.setContentsMargins(12, 6, 12, 6)
        layout.setSpacing(8)

        self.btn_refresh_toolbar = QPushButton("⟳  Refresh Ports")
        self.btn_refresh_toolbar.setObjectName("btnSecondary")
        self.btn_refresh_toolbar.clicked.connect(self._refresh_ports)

        self.btn_export = QPushButton("⬇  Export CSV")
        self.btn_export.setObjectName("btnSecondary")
        self.btn_export.clicked.connect(self._export_csv)

        self.btn_clear = QPushButton("✕  Clear")
        self.btn_clear.setObjectName("btnSecondary")
        self.btn_clear.clicked.connect(self._clear_data)

        self.btn_dark = QPushButton("◑  Dark Mode")
        self.btn_dark.setObjectName("btnToggle")
        self.btn_dark.setCheckable(True)
        self.btn_dark.setChecked(True)
        self.btn_dark.clicked.connect(self._toggle_theme)

        title = QLabel("Industrial Production Monitor")
        title.setObjectName("appTitle")
        title.setAlignment(Qt.AlignCenter)

        layout.addWidget(self.btn_refresh_toolbar)
        layout.addWidget(self.btn_export)
        layout.addWidget(self.btn_clear)
        layout.addWidget(self.btn_dark)
        layout.addStretch()
        layout.addWidget(title)
        layout.addStretch()
        return bar

    def _build_body(self):
        body = QWidget()
        layout = QHBoxLayout(body)
        layout.setContentsMargins(0, 0, 0, 0)
        layout.setSpacing(0)
        layout.addWidget(self._build_sidebar(), 0)
        layout.addWidget(self._build_main_area(), 1)
        return body

    def _build_sidebar(self):
        sidebar = QWidget()
        sidebar.setObjectName("sidebar")
        sidebar.setFixedWidth(310)
        layout = QVBoxLayout(sidebar)
        layout.setContentsMargins(12, 12, 12, 12)
        layout.setSpacing(10)

        layout.addWidget(self._section_header("Serial Connection"))

        btn_refresh_side = QPushButton("Refresh Ports")
        btn_refresh_side.setObjectName("btnSecondary")
        btn_refresh_side.clicked.connect(self._refresh_ports)
        layout.addWidget(btn_refresh_side)

        layout.addWidget(self._side_label("COM Port:"))
        self.combo_port = QComboBox()
        self.combo_port.setObjectName("sideCombo")
        layout.addWidget(self.combo_port)

        layout.addWidget(self._side_label("Baud Rate:"))
        self.combo_baud = QComboBox()
        self.combo_baud.setObjectName("sideCombo")
        for b in ["9600", "19200", "38400", "57600", "115200"]:
            self.combo_baud.addItem(b)
        layout.addWidget(self.combo_baud)

        self.btn_connect = QPushButton("Connect")
        self.btn_connect.setObjectName("btnConnect")
        self.btn_connect.clicked.connect(self._connect)
        layout.addWidget(self.btn_connect)

        self.btn_disconnect = QPushButton("Disconnect")
        self.btn_disconnect.setObjectName("btnDisconnect")
        self.btn_disconnect.clicked.connect(self._disconnect)
        layout.addWidget(self.btn_disconnect)

        layout.addWidget(self._section_header("Status"))
        self.lbl_status   = QLabel("Status: Disconnected")
        self.lbl_last_pkt = QLabel("Last Packet: —")
        self.lbl_pkt_cnt  = QLabel("Packets: 0")
        self.lbl_err_cnt  = QLabel("Errors: 0")
        self.lbl_status.setStyleSheet("color: #ff4444; font-weight: bold;")
        for lbl in [self.lbl_status, self.lbl_last_pkt,
                    self.lbl_pkt_cnt, self.lbl_err_cnt]:
            lbl.setObjectName("statusLabel")
            layout.addWidget(lbl)

        layout.addWidget(self._section_header("Activity Log"))
        self.log_box = QTextEdit()
        self.log_box.setObjectName("logBox")
        self.log_box.setReadOnly(True)
        layout.addWidget(self.log_box, 1)

        self.lbl_bottom = QLabel("Not connected")
        self.lbl_bottom.setObjectName("bottomBar")
        layout.addWidget(self.lbl_bottom)

        return sidebar

    def _build_main_area(self):
        area = QWidget()
        layout = QVBoxLayout(area)
        layout.setContentsMargins(12, 12, 12, 12)
        layout.setSpacing(10)
        layout.addLayout(self._build_stat_row())
        layout.addWidget(self._build_tabs(), 1)
        return area

    def _build_stat_row(self):
        row = QHBoxLayout()
        row.setSpacing(10)
        self.card_total  = StatCard("Total Production", "0",       "#ff4444")
        self.card_lines  = StatCard("Conveyor Lines",   "0",       "#00d4ff")
        self.card_pkts   = StatCard("Packets Received", "0",       "#00d4ff")
        self.card_port   = StatCard("Current COM Port", "N/A",     "#ff4444")
        self.card_clock  = StatCard("Last Update",      "—",       "#ff4444")
        self.card_uptime = StatCard("System Uptime",    "00:00:00","#ff4444")
        for card in [self.card_total, self.card_lines, self.card_pkts,
                     self.card_port, self.card_clock, self.card_uptime]:
            row.addWidget(card, 1)
        return row

    def _build_tabs(self):
        self.tabs = QTabWidget()
        self.tabs.setObjectName("mainTabs")
        self.tabs.addTab(self._build_prod_tab(),  "Production Table")
        self.tabs.addTab(self._build_24h_tab(),   "24-Hour View")
        self.tabs.addTab(self._build_production_plan_tab(),   "Production Plan")
        return self.tabs

    def _build_prod_tab(self):
        tab = QWidget()
        layout = QVBoxLayout(tab)
        layout.setContentsMargins(0, 8, 0, 0)
        layout.setSpacing(8)
        layout.addLayout(self._build_filter_bar())

        hours = [f"{h:02d}:00" for h in range(24)]
        cols  = ["Date", "Panel", "Zone", "Line Name", "Product Slno", "Part No", "Target"] + hours
        self.prod_table = self._make_table(cols)
        # Fixed widths
        for w, c in zip([100, 55, 55, 140, 90, 90, 60], range(7)):
            self.prod_table.setColumnWidth(c, w)
        for c in range(7, 7 + 24):
            self.prod_table.setColumnWidth(c, 46)
        layout.addWidget(self.prod_table, 1)
        return tab

    def _build_24h_tab(self):
        tab = QWidget()
        layout = QVBoxLayout(tab)
        layout.setContentsMargins(0, 8, 0, 0)
        hours = [f"{h:02d}:00" for h in range(24)]
        cols  = ["Panel", "Zone", "Conveyor", "Line Name", "Product Slno", "Part No"] + hours + ["Total"]
        self.table_24h = self._make_table(cols)
        self.table_24h.setColumnWidth(0, 55)
        self.table_24h.setColumnWidth(1, 55)
        self.table_24h.setColumnWidth(2, 70)
        self.table_24h.setColumnWidth(3, 140)
        self.table_24h.setColumnWidth(4, 90)
        self.table_24h.setColumnWidth(5, 90)
        for c in range(6, 6 + 24):
            self.table_24h.setColumnWidth(c, 46)
        layout.addWidget(self.table_24h, 1)
        return tab

    def _build_production_plan_tab(self):
        tab = QWidget()
        layout = QVBoxLayout(tab)
        layout.setContentsMargins(12, 12, 12, 12)
        layout.setSpacing(10)

        # Title
        title = QLabel("Production Plan - Machine Schedule")
        title.setObjectName("sectionHeader")
        layout.addWidget(title)

        # Scroll area for the form
        scroll = QScrollArea()
        scroll.setWidgetResizable(True)
        scroll.setObjectName("scrollArea")

        form_container = QWidget()
        form_layout = QVBoxLayout(form_container)
        form_layout.setContentsMargins(0, 0, 0, 0)
        form_layout.setSpacing(8)

        # Fetch machines and products
        machines = self._db.get_all_machines()
        products = self._db.get_all_products()
        product_list = [(p["slno"], p["partno"]) for p in products]

        # Fetch saved production plan
        saved_plan = self._db.get_production_plan()
        plan_dict = {row['mslno']: row for row in saved_plan}

        # Create form rows for each machine
        self._machine_dropdowns = {}
        self._machine_targets = {}
        if machines:
            for machine in machines:
                row_layout = QHBoxLayout()
                row_layout.setSpacing(10)

                # Machine mslno
                lbl_slno = QLabel(f"Machine {machine['mslno']}")
                lbl_slno.setMinimumWidth(100)
                row_layout.addWidget(lbl_slno)

                # Conveyor name
                lbl_conveyor = QLabel(machine['conveyorname'] or "—")
                lbl_conveyor.setMinimumWidth(150)
                row_layout.addWidget(lbl_conveyor)

                # Product dropdown
                combo = QComboBox()
                combo.setObjectName("productCombo")
                combo.addItem("Select Product", None)
                for product_slno, partno in product_list:
                    display_text = f"{product_slno} ({partno})" if partno else product_slno
                    combo.addItem(display_text, product_slno)
                
                # Load saved product if exists
                if machine['mslno'] in plan_dict:
                    saved_product = plan_dict[machine['mslno']]['productslno']
                    idx = combo.findData(saved_product)
                    if idx >= 0:
                        combo.setCurrentIndex(idx)
                
                row_layout.addWidget(combo, 1)

                # Production target input
                target_label = QLabel("Target:")
                target_label.setMinimumWidth(50)
                row_layout.addWidget(target_label)

                target_spinbox = QSpinBox()
                target_spinbox.setMinimum(0)
                target_spinbox.setMaximum(10000)
                target_spinbox.setValue(0)
                
                # Load saved target if exists
                if machine['mslno'] in plan_dict:
                    target_spinbox.setValue(plan_dict[machine['mslno']]['target'])
                
                target_spinbox.setMinimumWidth(120)
                row_layout.addWidget(target_spinbox)

                # Store references to this dropdown and target
                self._machine_dropdowns[machine['mslno']] = combo
                self._machine_targets[machine['mslno']] = target_spinbox

                form_layout.addLayout(row_layout)
        else:
            no_machines = QLabel("No machines configured. Add machines to the machine_list table.")
            no_machines.setStyleSheet("color: #888; font-style: italic;")
            form_layout.addWidget(no_machines)

        form_layout.addStretch()
        scroll.setWidget(form_container)
        layout.addWidget(scroll, 1)

        # Action buttons
        btn_layout = QHBoxLayout()
        btn_layout.setSpacing(10)

        btn_save = QPushButton("Save Plan")
        btn_save.setObjectName("btnConnect")
        btn_save.clicked.connect(self._save_production_plan)
        btn_layout.addWidget(btn_save)

        btn_clear = QPushButton("Clear")
        btn_clear.setObjectName("btnSecondary")
        btn_clear.clicked.connect(self._clear_production_plan)
        btn_layout.addWidget(btn_clear)

        btn_export_plan = QPushButton("⬇  Export Plan CSV")
        btn_export_plan.setObjectName("btnSecondary")
        btn_export_plan.clicked.connect(self._export_production_plan_manual)
        btn_layout.addWidget(btn_export_plan)

        btn_layout.addStretch()
        layout.addLayout(btn_layout)

        return tab

    def _build_filter_bar(self):
        row = QHBoxLayout()
        row.setSpacing(6)

        lbl = QLabel("Quick Filter:")
        lbl.setObjectName("filterLabel")
        row.addWidget(lbl)

        self._filter_btns = {}
        for label, mode in [("Today","today"),("Last 7 Days","7days"),
                             ("Last 30 Days","30days"),("All","all")]:
            btn = QPushButton(label)
            btn.setObjectName("filterBtn")
            btn.setCheckable(True)
            btn.setChecked(mode == "all")
            btn.clicked.connect(lambda _, m=mode: self._set_quick_filter(m))
            self._filter_btns[mode] = btn
            row.addWidget(btn)

        row.addSpacing(10)

        for lbl_txt, attr in [("Panel:", "combo_f_panel"),
                               ("Zone:",  "combo_f_zone"),
                               ("Conveyor:", "combo_f_conveyor")]:
            row.addWidget(QLabel(lbl_txt))
            cb = QComboBox()
            cb.setObjectName("filterCombo")
            cb.addItem("All")
            cb.currentIndexChanged.connect(lambda _: self._refresh_ui_from_db())
            setattr(self, attr, cb)
            row.addWidget(cb)

        row.addStretch()
        return row

    # ──────────────────────────────────────────
    #  Serial control
    # ──────────────────────────────────────────
    def _refresh_ports(self):
        self.combo_port.clear()
        ports = serial.tools.list_ports.comports()
        for p in ports:
            self.combo_port.addItem(f"{p.device} - {p.description}", p.device)
        self._log(f"Ports refreshed: {len(ports)} available")

    def _connect(self):
        if self._connected:
            return
        idx = self.combo_port.currentIndex()
        if idx < 0:
            QMessageBox.warning(self, "No Port", "Select a COM port first.")
            return
        port = self.combo_port.itemData(idx) or \
               self.combo_port.currentText().split(" - ")[0]
        baud = int(self.combo_baud.currentText())

        self._log(f"Connecting to {port}...")
        self._start_time = datetime.now()

        self._serial_thread = QThread()
        self._serial_worker = SerialWorker(port, baud)
        self._serial_worker.moveToThread(self._serial_thread)

        self._serial_thread.started.connect(self._serial_worker.run)
        self._serial_worker.line_received.connect(self._on_line_received)
        self._serial_worker.error_occurred.connect(self._on_serial_error)
        self._serial_worker.finished.connect(self._serial_thread.quit)
        self._serial_thread.finished.connect(self._on_thread_done)

        self._serial_thread.start()
        self._connected = True

        self._log("Serial worker started")
        self._log(f"Connected to {port} at {baud} baud")

        self.lbl_status.setText("Status: Connected")
        self.lbl_status.setStyleSheet("color: #00ff88; font-weight: bold;")
        self.card_port.set_value(port)
        self.lbl_bottom.setText(f"Connected to {port} at {baud} baud")

    def _disconnect(self):
        if not self._connected:
            return
        if self._serial_worker:
            self._serial_worker.stop()
        self._connected = False
        self.lbl_status.setText("Status: Disconnected")
        self.lbl_status.setStyleSheet("color: #ff4444; font-weight: bold;")
        self.card_port.set_value("N/A")
        self.lbl_bottom.setText("Not connected")
        self._log("Disconnected")

    def _on_thread_done(self):
        self._connected = False

    # ──────────────────────────────────────────
    #  STEP 1→2→3: Receive → Parse → INSERT → SELECT → Render
    # ──────────────────────────────────────────
    def _on_line_received(self, raw_line: str):
        """
        Called (in the main/GUI thread via signal) for every raw serial line.

        Pipeline:
            raw_line  →  parse_packet()  →  db.insert()  →  _refresh_ui_from_db()
        """
        self._packets_rx += 1
        ts = datetime.now().strftime("%H:%M:%S")
        self._log(f"RX raw: {raw_line}")

        # ── STEP 1a: Clean garbage bytes from the raw line ────
        cleaned_line = clean_line(raw_line)
        if not cleaned_line:
            self._on_serial_error("Empty line after cleaning")
            self.lbl_pkt_cnt.setText(f"Packets: {self._packets_rx}")
            return
        if cleaned_line != raw_line:
            self._log(f"  → Cleaned: {cleaned_line}")

        # ── STEP 1b: Parse fields ─────────────────────────────
        fields = parse_packet(cleaned_line)
        if fields is None:
            self._on_serial_error(f"Invalid packet: {cleaned_line}")
            self.lbl_pkt_cnt.setText(f"Packets: {self._packets_rx}")
            return

        self._log(
            f"  → panel={fields['panel_id']}  date={fields['year']}-"
            f"{fields['month']:02d}-{fields['day']:02d}  "
            f"time={fields['hour']:02d}:{fields['minute']:02d}:{fields['second']:02d}  "
            f"zone={fields['zone']}  conveyor={fields['conveyor_name']}  "
            f"count={fields['count']}"
        )

        # ── STEP 2: INSERT into SQLite ────────────────────────
        new_id = self._db.insert(fields)
        self._log(f"  → Saved to DB (ID={new_id})")

        # ── STEP 3: SELECT from SQLite → render UI ────────────
        self._refresh_ui_from_db()

        # Update sidebar counters
        self.lbl_last_pkt.setText(f"Last Packet: {ts}")
        self.lbl_pkt_cnt.setText(f"Packets: {self._packets_rx}")
        self._tick_clock()

    def _on_serial_error(self, msg: str):
        self._errors += 1
        self.lbl_err_cnt.setText(f"Errors: {self._errors}")
        self._log(f"ERROR: {msg}")

    # ──────────────────────────────────────────
    #  STEP 3: SELECT from DB → render tables
    # ──────────────────────────────────────────
    def _refresh_ui_from_db(self):
        """
        Queries the database and re-renders both tables and stat cards.
        This is the ONLY place that reads from the DB to update the UI.
        """
        panel    = self.combo_f_panel.currentText()
        zone     = self.combo_f_zone.currentText()
        conveyor = self.combo_f_conveyor.currentText()

        # ── Stat cards (from aggregate SELECT) ───────────────
        stats = self._db.query_stats()
        self.card_total.set_value(stats["total_count"])
        self.card_lines.set_value(stats["line_count"])
        self.card_pkts.set_value(stats["total_rows"])

        # ── Production Table tab ──────────────────────────────
        rows = self._db.query_production_table(
            quick_filter=self._quick_filter,
            panel_id=panel, zone=zone, conveyor=conveyor,
        )
        self.prod_table.setRowCount(len(rows))
        for r_idx, row in enumerate(rows):
            try:
                d = date(row["year"], row["month"], row["day"])
                date_str = d.strftime("%d-%b-%Y")
            except ValueError:
                date_str = f"{row['year']}-{row['month']:02d}-{row['day']:02d}"

            line_name = row["conveyor"]
            cells = [
                date_str,
                row["panel_id"],
                row["zone"],
                line_name,
                row["productslno"] or "",
                row["partno"]      or "",
                str(row["target"]) or "0",
            ] + [str(row[f"hour_{h}"]) for h in range(24)]

            for c_idx, val in enumerate(cells):
                item = QTableWidgetItem(val)
                item.setTextAlignment(Qt.AlignCenter)
                self.prod_table.setItem(r_idx, c_idx, item)

        # ── 24-Hour View tab ──────────────────────────────────
        rows_24h = self._db.query_24h_view(
            quick_filter=self._quick_filter,
            panel_id=panel, zone=zone, conveyor=conveyor,
        )
        self.table_24h.setRowCount(len(rows_24h))
        for r_idx, row in enumerate(rows_24h):
            line_name = row["conveyor"]
            cells = [
                row["panel_id"],
                row["zone"],
                row["conveyor"],
                line_name,
                row["productslno"] or "",
                row["partno"]      or "",
            ] + [str(row[f"hour_{h}"]) for h in range(24)] + [str(row["period_total"])]

            for c_idx, val in enumerate(cells):
                item = QTableWidgetItem(val)
                item.setTextAlignment(Qt.AlignCenter)
                self.table_24h.setItem(r_idx, c_idx, item)

        # ── Filter dropdowns (repopulate without losing selection) ──
        self._repopulate_filter_combos()

    def _repopulate_filter_combos(self):
        distinct = self._db.query_distinct_values()
        for combo, key in [(self.combo_f_panel, "panels"),
                           (self.combo_f_zone,  "zones"),
                           (self.combo_f_conveyor, "conveyors")]:
            current = combo.currentText()
            combo.blockSignals(True)
            combo.clear()
            combo.addItem("All")
            for val in distinct[key]:
                combo.addItem(val)
            idx = combo.findText(current)
            combo.setCurrentIndex(max(0, idx))
            combo.blockSignals(False)

    # ──────────────────────────────────────────
    #  Filter control
    # ──────────────────────────────────────────
    def _set_quick_filter(self, mode: str):
        self._quick_filter = mode
        for m, btn in self._filter_btns.items():
            btn.setChecked(m == mode)
        self._refresh_ui_from_db()

    # ──────────────────────────────────────────
    #  Toolbar actions
    # ──────────────────────────────────────────
    def _clear_data(self):
        reply = QMessageBox.question(
            self, "Clear All Data",
            "This will permanently delete ALL records from the database.\nContinue?",
            QMessageBox.Yes | QMessageBox.No, QMessageBox.No,
        )
        if reply != QMessageBox.Yes:
            return
        self._db.delete_all()
        self._packets_rx = 0
        self._errors     = 0
        self.lbl_pkt_cnt.setText("Packets: 0")
        self.lbl_err_cnt.setText("Errors: 0")
        self.log_box.clear()
        self._refresh_ui_from_db()
        self._log("Database cleared")

    def _export_csv(self):
        path, _ = QFileDialog.getSaveFileName(
            self, "Export CSV", "production_data.csv", "CSV Files (*.csv)")
        if not path:
            return
        try:
            self._db.export_csv(path)
            self._log(f"Exported to {path}")
        except Exception as e:
            QMessageBox.critical(self, "Export Error", str(e))

    # ──────────────────────────────────────────
    #  Production Plan actions
    # ──────────────────────────────────────────
    def _export_production_plan_manual(self):
        """Manual on-demand export of the current production plan to a user-chosen CSV path."""
        path, _ = QFileDialog.getSaveFileName(
            self, "Export Production Plan", "production_plan.csv", "CSV Files (*.csv)"
        )
        if not path:
            return
        try:
            count = self._db.export_production_plan_csv(path)
            self._log(f"Production plan exported to {path} ({count} rows)")
            QMessageBox.information(self, "Exported", f"Exported {count} row(s) to:\n{path}")
        except Exception as e:
            QMessageBox.critical(self, "Export Error", str(e))
            self._log(f"Error exporting production plan: {e}")

    def _save_production_plan(self):
        """Save the selected products and targets for each machine to database."""
        if not hasattr(self, '_machine_dropdowns'):
            QMessageBox.information(self, "No Plan", "No machines configured.")
            return

        plan_data = {}
        for machine_slno, combo in self._machine_dropdowns.items():
            product_slno = combo.currentData()
            target = self._machine_targets[machine_slno].value()
            if product_slno:
                # Get conveyor name from the machine
                machines = self._db.get_all_machines()
                conveyor_name = ""
                for machine in machines:
                    if machine['mslno'] == machine_slno:
                        conveyor_name = machine['conveyorname']
                        break
                
                plan_data[machine_slno] = {
                    "conveyorname": conveyor_name,
                    "product": product_slno,
                    "target": target
                }

        if not plan_data:
            QMessageBox.warning(self, "Empty Plan", "Please select at least one product.")
            return

        # ── Export the EXISTING production plan to CSV before overwriting ──
        export_path = None
        try:
            existing_rows = self._db.get_production_plan()
            if existing_rows:
                os.makedirs("plan_backups", exist_ok=True)
                ts = datetime.now().strftime("%Y%m%d_%H%M%S")
                export_path = os.path.join(
                    "plan_backups", f"production_plan_backup_{ts}.csv"
                )
                count = self._db.export_production_plan_csv(export_path)
                self._log(f"Existing production plan backed up to {export_path} ({count} rows)")
        except Exception as e:
            # Backup failure should not block saving the new plan, but the
            # user must be told clearly so they can retry/export manually.
            self._log(f"WARNING: Failed to back up existing plan: {e}")
            proceed = QMessageBox.question(
                self, "Backup Failed",
                f"Could not export the existing production plan to CSV:\n{e}\n\n"
                "Save the new plan anyway?",
                QMessageBox.Yes | QMessageBox.No, QMessageBox.No
            )
            if proceed != QMessageBox.Yes:
                return

        try:
            self._db.save_production_plan(plan_data)
            self._log(f"Production plan saved: {plan_data}")
            msg = "Production plan saved successfully!"
            if export_path:
                msg += f"\n\nPrevious plan backed up to:\n{export_path}"
            QMessageBox.information(self, "Success", msg)
        except Exception as e:
            QMessageBox.critical(self, "Save Error", str(e))
            self._log(f"Error saving production plan: {e}")

    def _clear_production_plan(self):
        """Clear all product selections and targets in the plan."""
        if not hasattr(self, '_machine_dropdowns'):
            return

        for combo in self._machine_dropdowns.values():
            combo.setCurrentIndex(0)
        for spinbox in self._machine_targets.values():
            spinbox.setValue(0)
        self._log("Production plan cleared")

    # ──────────────────────────────────────────
    #  Timers & theme
    # ──────────────────────────────────────────
    def _tick_clock(self):
        self.card_clock.set_value(datetime.now().strftime("%Y-%m-%d %H:%M:%S"))

    def _tick_uptime(self):
        secs = int((datetime.now() - self._start_time).total_seconds())
        h, r = divmod(secs, 3600)
        m, s = divmod(r, 60)
        self.card_uptime.set_value(f"{h:02d}:{m:02d}:{s:02d}")

    def _toggle_theme(self):
        self._dark = self.btn_dark.isChecked()
        self._apply_theme()

    def _apply_theme(self):
        self.setStyleSheet(DARK_QSS if self._dark else LIGHT_QSS)

    # ──────────────────────────────────────────
    #  Helpers
    # ──────────────────────────────────────────
    def _log(self, msg: str):
        ts = datetime.now().strftime("%H:%M:%S")
        self.log_box.append(f"[{ts}] {msg}")

    def _section_header(self, text: str) -> QLabel:
        lbl = QLabel(text)
        lbl.setObjectName("sectionHeader")
        return lbl

    def _side_label(self, text: str) -> QLabel:
        lbl = QLabel(text)
        lbl.setObjectName("sideLabel")
        return lbl

    @staticmethod
    def _make_table(columns: list[str]) -> QTableWidget:
        t = QTableWidget()
        t.setObjectName("prodTable")
        t.setColumnCount(len(columns))
        t.setHorizontalHeaderLabels(columns)
        t.setRowCount(0)
        t.horizontalHeader().setSectionResizeMode(QHeaderView.Interactive)
        t.horizontalHeader().setStretchLastSection(False)
        t.verticalHeader().setVisible(False)
        t.setSelectionBehavior(QAbstractItemView.SelectRows)
        t.setEditTriggers(QAbstractItemView.NoEditTriggers)
        t.setAlternatingRowColors(True)
        return t

    def closeEvent(self, event):
        self._disconnect()
        self._db.close()
        event.accept()


# ═══════════════════════════════════════════════════════════════
#  Stylesheets
# ═══════════════════════════════════════════════════════════════
DARK_QSS = """
QMainWindow, QWidget {
    background-color: #0d1117;
    color: #c9d1d9;
    font-family: 'Segoe UI', 'Inter', sans-serif;
    font-size: 12px;
}
#toolbar { background-color: #161b22; border-bottom: 1px solid #30363d; }
#appTitle { color: #c9d1d9; font-size: 14px; font-weight: bold; }
#sidebar { background-color: #0d1117; border-right: 1px solid #30363d; }
#sectionHeader {
    color: #8b949e; font-size: 11px; font-weight: bold;
    text-transform: uppercase; letter-spacing: 1px; padding-top: 6px;
}
#sideLabel { color: #8b949e; font-size: 11px; }
QComboBox, #sideCombo, #filterCombo {
    background-color: #161b22; color: #c9d1d9;
    border: 1px solid #30363d; border-radius: 4px;
    padding: 4px 8px; min-height: 24px;
}
QComboBox QAbstractItemView {
    background-color: #161b22; color: #c9d1d9;
    selection-background-color: #1f6feb;
}
QPushButton, #btnSecondary {
    background-color: #21262d; color: #c9d1d9;
    border: 1px solid #30363d; border-radius: 4px;
    padding: 5px 12px; min-height: 28px;
}
QPushButton:hover { background-color: #30363d; border-color: #8b949e; }
#btnConnect {
    background-color: #1a7f37; color: #fff; border: none;
    border-radius: 4px; padding: 6px 14px; font-weight: bold; min-height: 30px;
}
#btnConnect:hover { background-color: #2ea043; }
#btnDisconnect {
    background-color: #b62324; color: #fff; border: none;
    border-radius: 4px; padding: 6px 14px; font-weight: bold; min-height: 30px;
}
#btnDisconnect:hover { background-color: #da3633; }
#btnToggle {
    background-color: #21262d; color: #c9d1d9;
    border: 1px solid #30363d; border-radius: 4px; padding: 5px 12px;
}
#btnToggle:checked { background-color: #1f6feb; color: #fff; border-color: #1f6feb; }
#statusLabel { color: #8b949e; font-size: 11px; }
#logBox {
    background-color: #0d1117; color: #8b949e;
    border: 1px solid #30363d; border-radius: 4px;
    font-family: 'Consolas','Courier New',monospace; font-size: 10px;
}
#bottomBar { color: #ff4444; font-size: 10px; padding: 2px 0; }
#statCard { background-color: #161b22; border: 1px solid #30363d; border-radius: 6px; }
#cardTitle { color: #8b949e; font-size: 11px; }
#cardValue { font-size: 20px; font-weight: bold; }
#mainTabs QTabBar::tab {
    background-color: #161b22; color: #8b949e;
    border: 1px solid #30363d; border-bottom: none;
    padding: 6px 16px; border-radius: 4px 4px 0 0;
}
#mainTabs QTabBar::tab:selected {
    background-color: #0d1117; color: #c9d1d9; border-bottom: 2px solid #1f6feb;
}
#mainTabs QTabWidget::pane { border: 1px solid #30363d; background-color: #0d1117; }
#filterLabel { color: #8b949e; }
#filterBtn {
    background-color: #21262d; color: #8b949e;
    border: 1px solid #30363d; border-radius: 4px; padding: 4px 10px;
}
#filterBtn:checked { background-color: #1f6feb; color: #fff; border-color: #1f6feb; }
#prodTable {
    background-color: #0d1117; color: #c9d1d9;
    gridline-color: #30363d; border: 1px solid #30363d;
    selection-background-color: #1f3a5f;
    alternate-background-color: #111820;
}
#prodTable QHeaderView::section {
    background-color: #161b22; color: #8b949e;
    border: none; border-right: 1px solid #30363d;
    border-bottom: 1px solid #30363d; padding: 4px 6px; font-size: 11px;
}
QScrollBar:horizontal, QScrollBar:vertical { background: #161b22; width: 8px; height: 8px; }
QScrollBar::handle { background: #30363d; border-radius: 4px; }
QScrollBar::add-line, QScrollBar::sub-line { width: 0; height: 0; }
"""

LIGHT_QSS = """
QMainWindow, QWidget {
    background-color: #f6f8fa; color: #24292f;
    font-family: 'Segoe UI','Inter',sans-serif; font-size: 12px;
}
#toolbar { background-color: #fff; border-bottom: 1px solid #d0d7de; }
#appTitle { color: #24292f; font-size: 14px; font-weight: bold; }
#sidebar { background-color: #fff; border-right: 1px solid #d0d7de; }
#sectionHeader {
    color: #57606a; font-size: 11px; font-weight: bold;
    text-transform: uppercase; letter-spacing: 1px; padding-top: 6px;
}
#sideLabel { color: #57606a; font-size: 11px; }
QComboBox, #sideCombo, #filterCombo {
    background-color: #fff; color: #24292f;
    border: 1px solid #d0d7de; border-radius: 4px;
    padding: 4px 8px; min-height: 24px;
}
QComboBox QAbstractItemView {
    background-color: #fff; color: #24292f;
    selection-background-color: #0969da; selection-color: #fff;
}
QPushButton {
    background-color: #f6f8fa; color: #24292f;
    border: 1px solid #d0d7de; border-radius: 4px;
    padding: 5px 12px; min-height: 28px;
}
QPushButton:hover { background-color: #eaeef2; }
#btnConnect {
    background-color: #2da44e; color: #fff; border: none;
    border-radius: 4px; padding: 6px 14px; font-weight: bold; min-height: 30px;
}
#btnConnect:hover { background-color: #2c974b; }
#btnDisconnect {
    background-color: #cf222e; color: #fff; border: none;
    border-radius: 4px; padding: 6px 14px; font-weight: bold; min-height: 30px;
}
#btnDisconnect:hover { background-color: #a40e26; }
#btnToggle {
    background-color: #f6f8fa; color: #24292f;
    border: 1px solid #d0d7de; border-radius: 4px; padding: 5px 12px;
}
#btnToggle:checked { background-color: #0969da; color: #fff; border-color: #0969da; }
#statusLabel { color: #57606a; font-size: 11px; }
#logBox {
    background-color: #f6f8fa; color: #57606a;
    border: 1px solid #d0d7de; border-radius: 4px;
    font-family: 'Consolas','Courier New',monospace; font-size: 10px;
}
#bottomBar { color: #1a7f37; font-size: 10px; padding: 2px 0; }
#statCard { background-color: #fff; border: 1px solid #d0d7de; border-radius: 6px; }
#cardTitle { color: #57606a; font-size: 11px; }
#cardValue { font-size: 20px; font-weight: bold; }
#mainTabs QTabBar::tab {
    background-color: #f6f8fa; color: #57606a;
    border: 1px solid #d0d7de; border-bottom: none;
    padding: 6px 16px; border-radius: 4px 4px 0 0;
}
#mainTabs QTabBar::tab:selected {
    background-color: #fff; color: #24292f; border-bottom: 2px solid #0969da;
}
#mainTabs QTabWidget::pane { border: 1px solid #d0d7de; background-color: #fff; }
#filterLabel { color: #57606a; }
#filterBtn {
    background-color: #f6f8fa; color: #57606a;
    border: 1px solid #d0d7de; border-radius: 4px; padding: 4px 10px;
}
#filterBtn:checked { background-color: #0969da; color: #fff; border-color: #0969da; }
#prodTable {
    background-color: #fff; color: #24292f;
    gridline-color: #d0d7de; border: 1px solid #d0d7de;
    selection-background-color: #dbe9ff; selection-color: #24292f;
    alternate-background-color: #f6f8fa;
}
#prodTable QHeaderView::section {
    background-color: #f6f8fa; color: #57606a;
    border: none; border-right: 1px solid #d0d7de;
    border-bottom: 1px solid #d0d7de; padding: 4px 6px; font-size: 11px;
}
"""


# ═══════════════════════════════════════════════════════════════
#  Entry point
# ═══════════════════════════════════════════════════════════════
def main():
    app = QApplication(sys.argv)
    app.setApplicationName("Industrial Production Monitor")
    win = IndustrialMonitor()
    win.show()
    sys.exit(app.exec_())


if __name__ == "__main__":
    main()
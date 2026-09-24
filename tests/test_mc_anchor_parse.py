"""
Verification for the M/C No. anchor fix in main.py parse_packet().
Uses the real corrupted lines from the 23-Sep-2026 COM3 stream.
Run:  python3 tests/test_mc_anchor_parse.py
"""
import os
import sys
import tempfile
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from main import parse_packet, DatabaseManager


FAILURES = []

def check(name, cond, detail=""):
    status = "PASS" if cond else "FAIL"
    print(f"[{status}] {name}" + (f"  ({detail})" if detail and not cond else ""))
    if not cond:
        FAILURES.append(name)


# ── 1. Real corrupted lines from the live stream ──────────────
# 2 junk prefix fields (leftover from previous line over LoRa)
p = parse_packet("999,453,1,26,9,23,20,35,18,1,M/C No.01/850,453")
check("junk-prefix x2 parses (M/C 01/850)", p is not None and p["conveyor_name"] == "M/C No.01/850" and p["count"] == 453)
check("junk-prefix year 26 -> 2026", p is not None and p["year"] == 2026)
check("junk-prefix zone=1 panel=1", p is not None and p["panel_id"] == "1" and p["zone"] == "1")
check("junk-prefix time 20:35:18", p is not None and (p["hour"], p["minute"], p["second"]) == (20, 35, 18))

p = parse_packet("999999999999999999999999,7844,1,26,9,23,20,34,55,1,M/C No.02/660,7844")
check("huge junk prefix parses (M/C 02/660)", p is not None and p["conveyor_name"] == "M/C No.02/660" and p["count"] == 7844)

p = parse_packet("2155,1454,1,26,9,23,20,34,58,1,M/C No.03/650,1454")
check("junk-prefix parses (M/C 03/650)", p is not None and p["conveyor_name"] == "M/C No.03/650" and p["count"] == 1454)

p = parse_packet("8080,387,1,26,9,23,20,35,0,2,M/C No.06/550,387")
check("junk-prefix parses (M/C 06/550)", p is not None and p["conveyor_name"] == "M/C No.06/550" and p["zone"] == "2" and p["count"] == 387)

p = parse_packet("n6626,2621,1,26,9,23,20,35,2,2,M/C No.07/450,2621")
check("letter junk prefix parses (M/C 07/450)", p is not None and p["conveyor_name"] == "M/C No.07/450" and p["count"] == 2621)

# Clean lines (no junk) — must parse exactly as before
p = parse_packet("1,26,9,23,20,35,4,2,M/C No.08/450,10087")
check("clean line parses (M/C 08/450)", p is not None and p["conveyor_name"] == "M/C No.08/450" and p["count"] == 10087 and p["panel_id"] == "1")

p = parse_packet("1,26,9,23,20,35,6,3,M/C No.09/450,19")
check("clean line parses (M/C 09/450)", p is not None and p["count"] == 19 and p["zone"] == "3")

# ── 2. Legacy format still works (tests use it) ───────────────
p = parse_packet("1,2025,5,23,16,21,17,1,Conveyor Line 1,13")
check("legacy format parses (fallback anchor)", p is not None and p["conveyor_name"] == "Conveyor Line 1" and p["count"] == 13 and p["year"] == 2025)

p = parse_packet("1,2025,8,18,9,30,0,1,Line 1,10")
check("legacy format parses (Line 1)", p is not None and p["conveyor_name"] == "Line 1" and p["count"] == 10)

# ── 3. Garbage must still be rejected ─────────────────────────
check("junk fragment '621' rejected", parse_packet("621") is None)
check("junk fragment '2621' rejected", parse_packet("2621") is None)
check("junk fragment 'r' rejected", parse_packet("r") is None)
check("junk fragment 'n2621' rejected", parse_packet("n2621") is None)
check("empty rejected", parse_packet("") is None)
check("truncated packet rejected", parse_packet("1,26,9,23,20,35,6,3,M/C No.09/450") is None)
check("all-numeric junk rejected", parse_packet("999,453,1,26,9,23,20,35,18,1,453") is None)

# ── 4. End-to-end: clear + junk-prefixed stream → rows appear ──
def must_parse(line: str) -> dict:
    f = parse_packet(line)
    assert f is not None, f"failed to parse: {line}"
    return f


with tempfile.TemporaryDirectory() as tmpdir:
    DatabaseManager.DB_FILE = os.path.join(tmpdir, "production.db")
    db = DatabaseManager()

    # Simulate pre-clear traffic (baseline gets stored)
    db.insert(must_parse("1,26,9,23,20,35,4,2,M/C No.08/450,10087"))
    n_before = db._conn.execute("SELECT COUNT(*) FROM production").fetchone()[0]
    check("baseline insert works", n_before == 1)

    # Clear — must now reset baselines too
    db.delete_all()
    state_rows = db._conn.execute("SELECT COUNT(*) FROM machine_counter_state").fetchone()[0]
    check("delete_all clears machine_counter_state", state_rows == 0)

    # After Clear, baselines are gone, so each machine's FIRST packet
    # inserts its full count as a row (established first-packet behavior,
    # see test_machine_count_logic.py::test_same_count_is_ignored), and
    # subsequent packets insert only deltas.
    r1 = db.insert(must_parse("1,26,9,23,20,36,4,2,M/C No.08/450,10087"))
    r2 = db.insert(must_parse("999,457,1,26,9,23,20,37,4,2,M/C No.01/850,457"))
    r3 = db.insert(must_parse("999,459,1,26,9,23,20,38,4,2,M/C No.01/850,459"))
    n_after = db._conn.execute("SELECT COUNT(*) FROM production").fetchone()[0]
    check("post-clear: first packet (clean line) inserts row", r1 > 0)
    check("post-clear: junk-prefix first packet inserts row", r2 > 0)
    check("post-clear: junk-prefix increased count inserts delta row", r3 > 0)
    check("post-clear: production table has rows again", n_after == 3)

    # The delta row stored for the 457 -> 459 increase must be exactly 2
    delta = db._conn.execute(
        "SELECT count FROM production WHERE ID = ?", (r3,)
    ).fetchone()[0]
    check("post-clear: delta row value is 2 (459-457)", delta == 2)

    # Today filter sees the new rows (2 machine groups)
    rows = db.query_production_table(quick_filter="today")
    check("Today filter returns rows", len(rows) == 2)

    db.close()

print()
if FAILURES:
    print(f"RESULT: {len(FAILURES)} FAILED -> {FAILURES}")
    sys.exit(1)
print("RESULT: ALL CHECKS PASSED")

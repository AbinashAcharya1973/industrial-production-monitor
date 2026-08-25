# Industrial Production Monitor - Project Analysis

## Project Overview

**Industrial Production Monitor** is a PyQt5-based desktop application for real-time monitoring and visualization of production data from conveyor lines. It receives production metrics via serial communication (UART/USB), parses the data, stores it in SQLite, and displays it through an interactive dashboard with filtering and reporting capabilities.

**Technology Stack:**
- **Python 3** with PyQt5 (GUI framework)
- **SQLite3** (data persistence)
- **pyserial** (serial port communication)
- **CSV** (data import/export)
- **PyInstaller** (for building standalone executables)

---

## Architecture & Data Flow

### Data Pipeline (One-Way Flow)
```
SERIAL RX → Parse Fields → INSERT into SQLite → SELECT from SQLite → Render UI Table
```

### 1. **Serial Input Stage**
- **File:** `main.py` → `SerialWorker` class
- **Purpose:** Reads raw binary data from serial port in a separate QThread
- **Key Features:**
  - Handles UTF-8 and Latin-1 encoding with fallback
  - Splits multiple packets (handles both `\r\n` and literal `\\r\\n`)
  - Emits signals for each valid line received
  - Non-blocking async operation

### 2. **Data Parsing Stage**
- **Files:** `main.py` → `clean_line()` and `parse_packet()` functions
- **Purpose:** Convert raw serial line into structured field dictionary

**Packet Format:**
```
PanelID,YY,MM,DD,HH,MM,SS,ZoneID,ConveyorID,Count
Example: 1,25,5,23,16,21,17,1,"Conveyor Line 1",13
```

**Parsing Logic:**
1. **`clean_line()`** - Data hygiene:
   - Removes non-printable/non-ASCII characters (handles corrupted UTF-8 bytes)
   - Finds first digit (real data always starts with digit)
   - Handles common corruption patterns:
     - Binary garbage (decoded as replacement chars)
     - Latin-1 prefix bytes

2. **`parse_packet()`** - Field extraction:
   - Splits by comma, validates 10 fields
   - Converts numeric fields (panel_id, year, month, etc.)
   - **Normalizes conveyor names** (removes trailing \x00, collapses whitespace)
     - Critical for grouping: same physical machine could otherwise appear as duplicates in UI
   - Returns dict or None if invalid

### 3. **Data Storage Stage**
- **File:** `main.py` → `DatabaseManager` class
- **Database:** SQLite with 4 main tables

#### **Database Schema:**

**`production` table** (primary table)
```sql
ID              INTEGER PRIMARY KEY AUTOINCREMENT
panel_id        TEXT
year, month, day, hour, minute, second  INTEGER
zone            TEXT
conveyor        TEXT  -- normalized
count           INTEGER
productslno     VARCHAR  -- FK to products.slno
target          INTEGER  -- production target
```

**`products` table** (product reference)
```sql
slno    VARCHAR PRIMARY KEY
partno  VARCHAR  -- part number
```

**`machine_list` table** (machine metadata)
```sql
mslno           VARCHAR PRIMARY KEY  -- machine serial number
conveyorname    VARCHAR
```

**`production_plan` table** (planning data)
```sql
mslno           INTEGER PRIMARY KEY
conveyorname    VARCHAR
productslno     VARCHAR  -- which product on this machine
target          INTEGER  -- target count for shift/day
```

#### **Insertion Logic (Key Feature):**

The `DatabaseManager.insert()` method implements **smart delta calculation** for machine counters:

1. **Same count received twice** → Ignore (return -1)
2. **Count increases** → Insert only the **delta** (current - previous)
3. **Count decreases** → Treat as **reset**, update baseline, don't insert
4. **First count** → Insert as baseline

**Why this matters:**
- Conveyor counters are **monotonic** (only increase)
- This logic extracts incremental production per packet
- Handles counter resets (e.g., machine power cycle, shift reset)

**Machine identification:**
- Unique key: `(panel_id, zone, conveyor_name)`
- Tracks last count per machine to calculate delta

**Migrations built-in:**
- Auto-adds missing `productslno` and `target` columns
- Deduplicates products table (handles older DB format issues)
- Normalizes existing conveyor names

### 4. **Data Retrieval & Visualization Stage**
- **File:** `main.py` → `IndustrialMonitor` class (QMainWindow)
- **Three view modes:**

#### **Production Table Tab**
- Query: `query_production_table()`
- **Grouping:** `(panel_id, year, month, day, zone, conveyor, productslno, target)`
- **Display:** One row per unique machine per day with hourly breakdowns
  - Columns: Panel, Date, Zone, Conveyor, Product, Part No., Target, Hour 00-23, Day Total
- **Pivot aggregation:** `SUM(CASE WHEN hour=H THEN count)` for each hour
- Joined with products table to show part numbers

#### **24-Hour View Tab**
- Query: `query_24h_view()`
- **Grouping:** `(panel_id, zone, conveyor, productslno, target)` — collapses across all dates
- **Display:** Hourly totals aggregated over entire time range
- Same pivot logic but without date dimension

#### **Stat Cards (Dashboard):**
- Total Production (all counts)
- Conveyor Lines (distinct machines)
- Packets Received (serial packets processed)
- COM Port (active connection)
- Last Update (timestamp)
- System Uptime

### 5. **Filtering & Export**
- **Quick filters:** Today / Last 7 Days / Last 30 Days / All
- **Dropdown filters:** Panel ID, Zone, Conveyor name
- **Export functions:**
  - `export_csv()` - Full production data with product join
  - `export_production_plan_csv()` - Current production plan with targets
- **Dynamic filter dropdowns:** Populated from `DISTINCT` values in DB

---

## Key Features & Implementation

### ✅ **Strengths**

1. **Clean Architecture**
   - Strict separation: parsing → storage → retrieval → UI
   - DatabaseManager is single source of truth
   - Thread-safe with locks on all DB operations

2. **Robust Serial Handling**
   - Multiple encoding fallbacks (UTF-8 → Latin-1)
   - Handles packet corruption intelligently
   - Non-blocking async I/O

3. **Smart Counter Logic**
   - Automatic delta extraction from monotonic counters
   - Graceful reset handling
   - Per-machine state tracking without caching raw data

4. **Data Integrity**
   - Foreign key constraints enabled
   - Auto-migrations handle schema evolution
   - Deduplication logic for products table

5. **User Experience**
   - Dark/Light theme toggle
   - Real-time dashboard updates
   - Multi-tab view with different aggregations
   - Activity log with error highlighting
   - CSV export for further analysis

6. **Scalability**
   - SQLite with proper indexing strategy (via GROUP BY queries)
   - Pivot aggregation in SQL (not Python)
   - Thread-safe concurrent design

### ⚠️ **Areas for Consideration**

1. **Date Representation**
   - Year field changed from 2-digit (YY) to 4-digit (full year)
   - README still shows YY format - documentation mismatch
   - Ensure firmware/devices send full year

2. **Conveyor Name Normalization**
   - Happening at two stages: `clean_line()` and `parse_packet()`
   - Also mitigated by auto-migration
   - Could be simplified to single point

3. **Database Backup**
   - App stores in `production.db` (root directory)
   - Manual backups only (via export CSV)
   - Consider automatic backup strategy for production deployment

4. **Error Handling**
   - Activity log records errors
   - Some edge cases (malformed CSV, invalid dates) may pass silently
   - Could add validation layer

5. **Testing**
   - Only `test_machine_count_logic.py` exists (3 tests)
   - Tests cover delta calculation well
   - Missing: parsing edge cases, filtering, export functions, UI interactions

---

## File Structure

```
main.py                              # Main application (2000+ lines)
├── clean_line()                     # Serial data hygiene
├── parse_packet()                   # Field extraction
├── DatabaseManager                  # SQLite operations (400+ lines)
│   ├── _create_tables()            # Schema + migrations
│   ├── insert()                     # Smart delta insertion
│   ├── query_production_table()     # Hourly pivot by date
│   ├── query_24h_view()             # Hourly pivot (no date)
│   ├── query_distinct_values()      # Filter dropdowns
│   ├── query_stats()                # Dashboard stats
│   └── export_csv()                 # CSV export
├── SerialWorker                     # QThread for serial I/O
└── IndustrialMonitor               # Main QMainWindow UI (1000+ lines)
    ├── _build_ui()
    ├── _connect()/_disconnect()
    ├── _refresh_ui_from_db()
    ├── on_line_received()
    └── Filters & exports

requirements.txt                      # PyQt5, pyserial
main.spec                            # PyInstaller config
README.md                            # Documentation
tests/test_machine_count_logic.py   # Unit tests (delta logic)
```

---

## Database Migrations

The app includes **built-in schema evolution**:

1. **Auto-create** tables if missing (first run)
2. **Add missing columns** (`productslno`, `target`) to old databases
3. **Normalize conveyor names** across entire database
4. **Deduplicate products** table using window functions

This allows upgrades without manual SQL intervention.

---

## Usage Workflow

1. **Launch** application → UI initializes, loads existing DB
2. **Plug in serial device** (USB/UART)
3. **Select COM port** and **baud rate** from dropdowns
4. **Click Connect** → SerialWorker thread starts reading
5. **Packets arrive** → parsed → delta calculated → stored
6. **UI updates** every packet:
   - Production table refreshes
   - Stat cards update
   - Activity log shows events
7. **Apply filters** (date, panel, zone, conveyor)
8. **Export CSV** for further analysis
9. **Theme toggle** (Dark/Light mode)
10. **Disconnect** → serial thread cleanly stops

---

## Testing Coverage

**Tested:**
- ✅ Duplicate count detection (ignore)
- ✅ Delta extraction (increment)
- ✅ Reset detection (lower count)
- ✅ Initial count handling (first packet)

**Not yet tested:**
- ❌ Parsing edge cases (corrupted data, weird encodings)
- ❌ Database migrations
- ❌ Filtering logic
- ❌ Export functions
- ❌ UI interactions
- ❌ Serial port errors
- ❌ Production_plan joining

---

## Deployment Notes

**Building Standalone Executable:**
```bash
pyinstaller main.spec
```

**Output:** `dist/IndustrialMonitor/` or `dist/main/`

**Dependencies:**
- Python 3.8+
- PyQt5 5.15+
- pyserial 3.5+

**Database Location:**
- Reads/writes `production.db` in current directory
- Custom location can be set via `DatabaseManager.DB_FILE`

---

## Summary

This is a **well-architected, production-ready** application for industrial monitoring. The data pipeline is clean, the database operations are thread-safe, and the UI provides useful insights through multiple views and filters. The built-in smart counter logic elegantly handles monotonic sensor data without requiring device-side aggregation.

**Recommended next steps:**
1. Expand test coverage (especially parsing and edge cases)
2. Document the year format change in README
3. Consider backup strategy for production environments
4. Add logging/metrics for deployment monitoring

# Project Analysis

## What This Repository Is

This repository is an industrial production monitoring desktop app built with PyQt5 and SQLite.

The active application is [`main.py`](./main.py). It receives serial packets from conveyor-line hardware, parses them, stores them in SQLite, and renders two production views in the UI.

The README describes the same core idea:
- serial/UART/USB input
- hourly production table
- 24-hour summary view
- quick date filters
- COM port selection
- theme toggle
- CSV export

## Active App Behavior

### Data flow

The main pipeline is:
`SERIAL RX -> clean line -> parse packet -> insert into SQLite -> select from SQLite -> render UI`

### Packet format

The app expects packets shaped like:

`PanelID,Year,Month,Day,Hour,Minute,Second,ZoneID,ConveyorID,Count`

The README still shows an older year format example with a 2-digit year, but the current code in `main.py` parses a full year field.

### Serial handling

The app:
- enumerates available COM ports with `pyserial`
- lets the user choose a baud rate
- reads in a `QThread`
- decodes UTF-8 first, then falls back to Latin-1
- handles literal `\\r\\n` strings from firmware
- splits multi-line reads into individual packets

### Database behavior

`DatabaseManager` is the single source of truth for SQLite operations.

It manages:
- `products`
- `production`
- `machine_list`
- `production_plan`

Notable behaviors:
- it creates tables on startup
- it migrates older databases by adding missing `productslno` and `target` columns
- it normalizes conveyor names to prevent duplicate GROUP BY rows
- it deduplicates `products` if duplicate `slno` values are found
- it keeps an in-memory last-count baseline per machine
- it stores only the delta when counts increase
- it ignores repeated counts
- it treats count drops as resets

### UI layout

The app window contains:
- a top toolbar
- a left sidebar for serial connection and logs
- stat cards for total production, conveyor lines, packets received, COM port, last update, and uptime
- three tabs:
  - Production Table
  - 24-Hour View
  - Production Plan

### Production Table

This tab shows:
- date
- panel
- zone
- line name
- product serial number
- part number
- target
- hourly totals from 00:00 to 23:00

### 24-Hour View

This tab collapses totals across all dates and groups by:
- panel
- zone
- conveyor
- product
- target

### Production Plan

This tab is backed by `machine_list` and `production_plan`.

It lets the user:
- choose a product per machine
- enter a target value
- save the plan
- clear the form
- export the plan to CSV

When saving a plan, the app first attempts to back up the current production plan into `plan_backups/` with a timestamped CSV filename.

## Code Structure

### `main.py`

This is the active app and contains:
- `clean_line`
- `parse_packet`
- `DatabaseManager`
- `SerialWorker`
- `StatCard`
- `IndustrialMonitor`
- Qt stylesheet definitions
- the `main()` entry point

### `tests/test_machine_count_logic.py`

This test file verifies the counter logic in `DatabaseManager.insert()`:
- same count is ignored
- increasing count stores only the delta
- decreasing count is treated as a reset and does not insert a negative delta

The test suite currently passes.

### Historical copies / variants

The repository contains a lot of snapshot-style files:
- `main copy.py`
- `main copy 2.py` through `main copy 9.py`
- `main_before_claud_update.py`
- `main_wrongcopilot.py`
- `clietnt_side_imple.py`

These appear to be iterative copies or alternate versions of the same application, not separate products.

## On-Disk Data And Artifacts

### SQLite databases

There are two database files in the repo:
- `production.db`
- `production_bak.db`

Both currently contain the same table set:
- `machine_list`
- `production`
- `production_plan`
- `products`

Current row counts in `production.db`:
- `production`: 0
- `products`: 440
- `machine_list`: 17
- `production_plan`: 17

Current row counts in `production_bak.db`:
- `production`: 27
- `products`: 440
- `machine_list`: 17
- `production_plan`: 17

Important note:
- the live database schema on disk is not identical to the schema created in `main.py`
- the on-disk `production` table includes a `created_at` column, while the code-defined schema does not
- the app code adds `target` and `productslno` migrations, but the DB files look like they were produced by an earlier or externally modified schema

### CSV / text data

Files that look like captured production data or exports:
- `production_data.csv`
- `HMI-data.txt`
- `plan_backups/production_plan_backup_20260621_130503.csv`

`production_data.csv` contains exported production rows with columns like:
- `ID`
- `panel_id`
- `year`
- `month`
- `day`
- `hour`
- `minute`
- `second`
- `zone`
- `conveyor`
- `count`
- `productslno`
- `partno`
- `target`

`HMI-data.txt` contains serial macro/script snippets that emit packets using `SPRINTF`, `PUTCHARS`, and `DELAY`.

The `hmi_macros/` files are also packet-emission scripts for different panels:
- `panel1`
- `panel2`
- `panel3`

`list.txt` is currently empty.

### Archives and build outputs

There are several large archive/build artifacts in the repo:
- `Archive.zip`
- `Archive (1).zip`
- `Archive (2).zip`
- `app.zip`
- `build.zip`

There are also PyInstaller spec files:
- `main.spec`
- `IndustrialMonitor.spec`

Both bundle `main.py`, but they differ in the executable name and console/window settings.

## Readme vs Code

The README is broadly correct, but the code has grown beyond it:
- the Production Plan tab is implemented in code but not documented in the README
- plan backup export on save is implemented in code but not documented in the README
- the current packet parsing assumes a full year field
- conveyor names are cleaned more aggressively than the README describes

## Verification

Test run:
- `python -m pytest -q`
- result: `3 passed`

## Notable Observations

- The app is designed around SQLite as the only data source for UI rendering.
- `DatabaseManager.insert()` is the most important business-logic function because it turns raw counter readings into production deltas.
- The repository contains a lot of historical snapshots, so `main.py` should be treated as the source of truth unless the user explicitly wants a legacy version.
- There is no obvious package structure yet; the project is a single-file desktop app with tests and supporting data files.

## Suggested Next Cleanup

If this project is being maintained further, the biggest useful cleanup would be:
- remove or archive the duplicate `main copy*` files
- document the Production Plan feature in the README
- align the documented schema with the on-disk database schema
- decide which database file is the canonical starting point

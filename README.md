# Industrial Production Monitor

A PyQt5 desktop application for receiving and visualising production data
from conveyor lines over a serial (UART/USB) connection.

## Features
- Auto-detect and connect to any serial port at configurable baud rates
- Real-time packet parsing: `PanelID,YY,MM,DD,HH,MM,SS,ZoneID,ConveyorID,Count`
- Production Table with per-hour columns (00:00 – 23:00)
- 24-Hour View tab
- Quick filters: Today / Last 7 Days / Last 30 Days / All
- Dropdown filters: Date, Panel, Zone, Conveyor
- Live stat cards: Total Production, Conveyor Lines, Packets Received, COM Port, Last Update, System Uptime
- Activity log with timestamps and error highlighting
- Dark / Light theme toggle
- CSV export

## Setup

```bash
pip install -r requirements.txt
python main.py
```

## Data Format

Each packet is a comma-separated line terminated with `\r\n`:

```
PanelID,YY,MM,DD,HH,MM,SS,ZoneID,ConveyorID,Count
```

**Example:** `1,21,5,23,16,21,17,1,1,13`

| Field | Value | Meaning |
|-------|-------|---------|
| PanelID | 1 | Panel 1 |
| YY | 21 | Year 2021 |
| MM | 5 | May |
| DD | 23 | 23rd |
| HH | 16 | 16:00 |
| MM | 21 | :21 |
| SS | 17 | :17 |
| ZoneID | 1 | Zone 1 |
| ConveyorID | 1 | Conveyor 1 |
| Count | 13 | 13 units |

Multiple records may arrive in a single serial read, separated by `\r\n`
or the literal string `\r\n` — both are handled automatically.

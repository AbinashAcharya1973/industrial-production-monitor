# Industrial Production Monitor — User Manual

## 1. Purpose

Industrial Production Monitor receives production counter data from HMI/conveyor equipment through a serial COM port, stores it in SQLite, and displays production totals by machine, zone, date, and hour.

The application uses the **M/C No. field as the machine reference**, for example:

```text
M/C No.01/850
M/C No.06/550
M/C No.09/450
```

## 2. Main screen

The main window contains:

- Toolbar
- Serial connection panel
- Status and activity log panel
- Production summary cards
- Production Table tab
- 24-Hour View tab
- Production Plan tab

### Summary cards

| Card | Meaning |
|---|---|
| Total Production | Total production records stored in the database |
| Conveyor Lines | Number of distinct panel/zone/machine combinations in stored data |
| Packets Received | Number of production rows stored by the current application session/database query |
| Current COM Port | Connected serial port |
| Last Update | Current computer date and time shown by the application |
| System Uptime | Time since the application was started |

## 3. Connecting to the HMI

1. Connect the HMI/LoRa serial device to the computer.
2. Start the application.
3. Click **Refresh Ports**.
4. Select the required COM port, for example `COM3`.
5. Select the baud rate. The normal value is `9600`.
6. Click **Connect**.
7. Confirm that the status changes to **Status: Connected**.
8. Wait for valid packets to appear in the Activity Log and tables.

The application supports these baud rates:

```text
9600, 19200, 38400, 57600, 115200
```

To stop receiving data, click **Disconnect**.

## 4. HMI packet processing

The HMI commonly sends data in this logical format:

```text
Panel,Year,Month,Day,Hour,Minute,Second,Zone,M/C No.,Count
```

Example clean packet:

```text
1,26,10,5,17,50,58,2,M/C No.08/450,12106
```

The application also handles packets with corrupted or leftover prefix fields, such as:

```text
1022,4376,1,26,10,5,17,50,49,1,M/C No.01/850,4376
```

For this packet, the application identifies the fields relative to `M/C No.01/850` and reads:

- Panel: `1`
- Year: `26`
- Zone: `1`
- Machine: `M/C No.01/850`
- Count: `4376`

The application rejects fragments such as `621`, `2621`, `r`, and incomplete packets.

## 5. Date and time handling

The HMI date and time are used only for packet validation. Accepted production records are registered using the computer's current local date and time when the packet is received.

Therefore:

- HMI date/time does not determine the production date in the database.
- The computer clock and timezone must be correct.
- Packets received after midnight are stored under the new system date automatically.
- Hourly totals use the computer's current hour.

Example:

```text
Packet received at 00:05 on 07-Oct-2026
→ Stored as 07-Oct-2026, hour 00:00
```

## 6. Production counter behavior

The program tracks the latest counter value separately for each machine.

| Incoming counter condition | Application behavior |
|---|---|
| First value for a machine | Stores the full value and creates a production record |
| Same value as previous packet | Ignores it as a duplicate |
| Higher value | Stores only the increase/delta |
| Lower value | Treats it as a machine counter reset and updates the baseline without storing a negative value |

Example:

```text
Previous count: 100
New count: 108
Production recorded: 8
```

## 7. Production Table

The **Production Table** shows one aggregated row per date, panel, zone, machine, product, and target.

Columns include:

- Date
- Panel
- Zone
- Line Name / M/C No.
- Product Slno
- Part No
- Target
- Hourly totals from `00:00` through `23:00`

### Quick filters

- **Today** — current computer date only
- **Last 7 Days** — current date and previous six days
- **Last 30 Days** — current date and previous 29 days
- **All** — all stored production records

### Dropdown filters

The table can also be filtered by:

- Panel
- Zone
- Conveyor / M/C No.

Filters are applied to the Production Table and 24-Hour View.

## 8. 24-Hour View

The **24-Hour View** combines the selected date range into hourly totals per machine.

It is useful for reviewing total activity by hour across the selected filter period.

## 9. Production Plan

The **Production Plan** tab assigns a product and target to each configured machine.

For each machine:

1. Select the product from the product dropdown.
2. Enter the target value.
3. Click **Save Plan**.

The selected product and target are associated with the machine's configured conveyor name. New production records use this plan information when the machine name matches.

### Plan buttons

- **Save Plan** — saves the current selections
- **Clear** — clears the values currently shown in the form
- **Export Plan CSV** — exports the current plan

Before replacing an existing plan, the application attempts to create a backup under:

```text
plan_backups/
```

If the backup fails, the application asks whether the new plan should still be saved.

### Machine and product configuration

The Production Plan form reads machines from the `machine_list` database table and products from the `products` table. If no machines are configured, the form displays a message explaining that machines must be added to `machine_list`.

## 10. Exporting production data

The toolbar **Export CSV** button exports the same aggregated format as the Production Table.

The export includes:

```text
Date, Panel, Zone, Line Name, Product Slno, Part No, Target,
00:00 ... 23:00, Day Total
```

The export respects the currently selected:

- Quick filter
- Panel filter
- Zone filter
- Conveyor/M/C filter

To export:

1. Select the required filters.
2. Click **Export CSV**.
3. Select the destination filename.
4. Confirm the export.

The application reports the number of exported rows after completion.

## 11. Clearing production data

The toolbar **Clear** button permanently deletes:

- Production records
- Product records
- Machine counter baselines

It also resets the in-memory counter state.

A confirmation dialog appears before deletion.

### Important warning

After clearing, the first counter value received from each machine is treated as a new starting value. If the HMI sends a cumulative counter such as `4376`, the first record after Clear may record `4376` as the initial production value.

Use Clear only when starting a new database period or when intentionally resetting the records.

## 12. Activity Log

The Activity Log records:

- Serial connection and disconnection
- Raw received lines
- Cleaned lines
- Parsed packet details
- Database record IDs
- Invalid packets
- Serial errors
- Export operations
- Plan operations

A high error count can indicate:

- Incorrect baud rate
- Wrong COM port
- HMI/LoRa line-framing corruption
- Incomplete packets
- Unsupported packet format
- Device communication problems

Fragments such as `621` or `2621` are expected to be rejected when they are not complete packets.

## 13. Dark and Light mode

Click the **Dark Mode** button in the toolbar to toggle between dark and light themes.

The mode affects only the display. It does not affect data storage or packet processing.

## 14. Daily operation recommendation

Before starting production:

1. Confirm the computer date, time, and timezone.
2. Connect the HMI device.
3. Confirm the expected COM port.
4. Select the correct baud rate.
5. Click **Refresh Ports**.
6. Click **Connect**.
7. Confirm valid M/C numbers appear in the log.
8. Select **Today** for the current production view.
9. Confirm the hourly values are increasing for active machines.
10. Export a CSV report when required.

At the end of a reporting period:

1. Export the production CSV.
2. Export the Production Plan if required.
3. Keep the database file as the source record.
4. Do not use **Clear** unless a new data period is intentionally being started.

## 15. Troubleshooting

### COM port is not listed

- Check the USB/serial cable.
- Confirm the device driver is installed.
- Click **Refresh Ports**.
- Close other programs using the port.
- Reconnect the device.

### Connected but no valid rows appear

- Confirm the baud rate.
- Check the Activity Log for `Invalid packet` messages.
- Confirm that the HMI is transmitting complete packets.
- Confirm that packets contain a valid `M/C No.XX/YYY` field.
- Confirm the selected quick filter is not hiding older data.

### Data appears under the wrong date

- Check the computer date and time.
- Check the computer timezone.
- Restart the application after correcting the system clock.
- Remember that current versions use computer receive time, not HMI packet time.

### A machine appears more than once

- Check for inconsistent M/C names or hidden characters.
- Check for old data recorded before the M/C-anchored parser was installed.
- Export the data and inspect the Panel, Zone, and Line Name columns.
- If necessary, back up the database and clear or migrate the affected records.

### Production does not appear after Clear

- Wait for the next packet from the HMI.
- Confirm that the serial status is Connected.
- Check the Activity Log.
- The first packet from each machine establishes a new counter baseline.

## 16. Safe shutdown

To close the application:

1. Click **Disconnect** if connected.
2. Confirm that the status changes to Disconnected.
3. Close the application window.

The application also attempts to disconnect the serial worker and close the database during normal window shutdown.

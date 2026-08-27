---
name: secusys-hibob-attendance
description: >-
  Set up and run the Secusys → HiBob (Bob) time & attendance integration on the
  user's machine. Use this whenever someone wants to connect a Secusys time clock
  to HiBob, import or sync attendance / clock-in-out punches into Bob, schedule the
  nightly attendance load, validate the employee mapping, or troubleshoot the
  secusys_to_hibob loader. The user only has to provide their variables — HiBob
  service-user credentials, the folder where the nightly Secusys file lands, and
  (only if Secusys isn't exporting yet) their SQL Server details — and this skill
  collects those, runs a safe test, and sets up the nightly schedule. Trigger even
  when the user just says things like "set up the Bob attendance integration", "run
  the Secusys sync", "get our time-clock data into HiBob", or "why didn't last
  night's attendance load into Bob", without naming any files.
---

# Secusys → HiBob attendance integration

This skill installs and runs a small, dependency-free integration that reads the
nightly punch file Secusys exports, pairs each entrance/exit into a work shift,
looks employees up in HiBob (Bob), and creates the attendance entries via Bob's
public API. Your job is to gather the few variables the user must supply, run a
safe test, then schedule it to run every night.

> ⚠️ **Please note:** This is not an official or certified HiBob integration. It's
> shared purely as guidelines / a reference implementation to show how to import
> attendance punches into Bob via the public API, to help your IT team build the
> connection. Please review and test it in a sandbox before using it in production.

## Bundled files (already in this skill)

- `scripts/secusys_to_hibob.py` — the loader (Bob side). Pure Python 3.8+ stdlib.
- `scripts/secusys_export.sql` — SQL Server query that produces the nightly file
  (Secusys side; only needed if Secusys isn't already exporting one).
- `scripts/run_secusys_export.bat` — runs that SQL and writes the timestamped file.
- `assets/sample_extract.txt` — a safe example file for the first test.
- `references/setup-guide.md` — the full guide: troubleshooting, the exact API
  bodies, and the 30-character file format. Read it when you need detail beyond
  this workflow.

Copy the files out of the skill to a working folder on the user's machine before
running them (e.g. `C:\secusys-hibob\` on Windows or `/opt/secusys-hibob/` on
Linux), so scheduled tasks have a stable path.

## How to work with the user

Move one step at a time and **ask for each variable you don't already have** rather
than guessing. Never write the HiBob token into a file or into the script — it goes
in an environment variable. Confirm before creating scheduled tasks or writing to
system locations, since those are outward-facing changes.

## Step 0 — Which sides are needed?

Ask: **does Secusys already produce a nightly punch file** (often pushed by SFTP
around 02:00)?

- **Yes** → you only need the **Bob side** (Steps 2–6). Find out which folder that
  file lands in.
- **No / not sure** → set up the **Secusys export** first (Step 1), then the Bob
  side.

Also ask whether the machine is **Windows or Linux** — it decides how you set
environment variables and schedule the job.

## Step 1 — Secusys export (only if they don't have a nightly file)

The punches live in the Secusys SQL Server database. `scripts/secusys_export.sql`
emits, for each employee each day, the **first punch as the entrance and the last
punch as the exit** in the exact fixed-width format the loader reads.

Collect and fill in:
- The punches **table** name and its **employee-number** and **timestamp** columns
  (a DBA usually knows these). Replace the three `{{PLACEHOLDER}}` values in the SQL.
- `SERVER`, `DATABASE`, and an output folder `OUTDIR` in `run_secusys_export.bat`.

Have them run it once and eyeball the output — lines should look like
`0000003680000012-08-260010913B`. Then schedule `run_secusys_export.bat` nightly
(~02:00). The loader's inbox (Step 5) points at the same `OUTDIR`.

## Step 2 — Prerequisites

Confirm **Python 3.8+** (`python3 --version`, or `python --version` on Windows) on
the machine that receives the file. Nothing else needs installing.

Ask the user to create a **HiBob service user** (Bob → Settings → Integrations →
Service users → New service user) with permission to **read employees (People)**
and **create/manage attendance**, and to give you its **Service User ID** and
**Token**. Also confirm that each employee's Secusys number equals their **Employee
ID** (`employeeIdInCompany`) in Bob — if it lives in a different Bob field, note it
for Step 5's `--bob-match-field`.

## Step 3 — Set the credentials (environment variables)

The loader reads secrets from the environment so nothing sensitive is stored.

Windows (PowerShell):
```
setx HIBOB_SERVICE_USER_ID "<id>"
setx HIBOB_SERVICE_USER_TOKEN "<token>"
```
Linux/Mac:
```
export HIBOB_SERVICE_USER_ID="<id>"
export HIBOB_SERVICE_USER_TOKEN="<token>"
```
For a scheduled task, set them for the account the task runs under (on Windows,
`setx` sets them for the current user; for a service account use its context).

## Step 4 — Safe dry runs (writes nothing)

1. **Offline** — prove the file parses:
   ```
   python3 secusys_to_hibob.py --file assets/sample_extract.txt --dry-run
   ```
   Expect a clean shift list (e.g. employee 368 → 09:13–18:54) with a
   `<lookup Secusys #…>` placeholder where the Bob id will go.
2. **With credentials** — validate the mapping against Bob without creating
   anything. Point it at a real file the customer supplies:
   ```
   python3 secusys_to_hibob.py --file <real time_*.txt> --dry-run
   ```
   Read out any Secusys numbers it reports as unmatched — those employees need
   their Employee ID fixed in Bob (or a different `--bob-match-field`).

## Step 5 — One real test day

Run it for real on one file (no `--dry-run`). Add `--bob-match-field <dot.path>`
only if the number lives in a non-default Bob field; add `--include-inactive` to
also match terminated employees.
```
python3 secusys_to_hibob.py --file <real time_*.txt>
```
Have the user open an employee in Bob → Attendance and confirm the shift appears.

## Step 6 — Schedule it nightly

The loader reads the newest `time_*.txt` in a folder, sends it, and archives it:
```
python3 secusys_to_hibob.py --inbox <incoming-folder> --archive <done-folder> --log-file <log>
```
Run this a bit **after** the export (e.g. 02:30 if the file arrives ~02:00) — the
export must finish first.

- **Windows:** Task Scheduler → Daily 02:30 → Program `python`, Arguments
  `"<path>\secusys_to_hibob.py" --inbox "<incoming>" --archive "<done>"`.
- **Linux:** `crontab -e` →
  `30 2 * * * /usr/bin/python3 <path>/secusys_to_hibob.py --inbox <incoming> --archive <done> --log-file <log>`.

If you also set up Step 1, that's **two nightly jobs**: export first (~02:00), then
loader (~02:30).

## Variables to collect (quick reference)

| Variable | Needed when | Example |
|----------|-------------|---------|
| `HIBOB_SERVICE_USER_ID` / `..._TOKEN` | always | from the Bob service user |
| Incoming (inbox) folder | always | `D:\secusys\incoming` |
| Archive folder | always | `D:\secusys\done` |
| OS (Windows/Linux) | always | Windows |
| `--bob-match-field` | only if Secusys # isn't Bob's Employee ID | `/custom/table/123…` |
| SQL `SERVER` / `DATABASE` / punches table + columns / `OUTDIR` | only if setting up the export | `SQL01` / `Secusys` / `dbo.tblEvents` |

## When something goes wrong

Read `references/setup-guide.md` (troubleshooting table + the exact API request
bodies). Common cases: `HTTP 401` → wrong id/token or missing permissions;
`people/search returned no employees` → missing People read permission; unmatched
numbers → Employee ID mismatch in Bob; failed run → the file is left in place and
retried next run.

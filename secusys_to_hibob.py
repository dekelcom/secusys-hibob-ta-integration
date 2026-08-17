#!/usr/bin/env python3
"""
Secusys -> HiBob (Bob) | Time & Attendance loader
=================================================================================
Secusys runs on-prem and drops a nightly fixed-width text extract (pushed over
SFTP ~02:00). This script picks up that file, pairs each entrance/exit punch into
a work shift, resolves each employee against Bob, and creates the attendance
entries in HiBob.

  Secusys server ──SFTP nightly──▶  landing dir  ──▶  this script  ──▶  HiBob API

HiBob APIs used (per HiBob product guidance):
  1. POST /v1/people/search       -> map Secusys employee number to Bob employee id
  2. POST /v1/attendance/entries  -> create the clock-in/out entries

Pure standard-library Python 3.8+ — nothing to pip install. Run it from cron
(Linux) or Task Scheduler (Windows) shortly after the file lands.

---------------------------------------------------------------------------------
RECORD LAYOUT  (each valid line is exactly 30 characters)
---------------------------------------------------------------------------------
  offset  width  field                     example      -> meaning
  0-8     9      employee number (padded)  000000368    -> 368
  9-13    5      filler (always 00000)     00000        -> ignored
  14-21   8      date  DD-MM-YY            12-08-26     -> 2026-08-12
  22-24   3      reader / terminal id      001          -> ignored (kept for logs)
  25-28   4      time  HHMM                0913         -> 09:13
  29      1      direction                 B            -> B=entrance, E=exit

  Lines that are not 30 chars long (e.g. a punch with no employee number) or whose
  employee number is 0 are skipped and counted.

  If a future extract uses different column widths, adjust the *_SLICE constants
  below; nothing else needs to change.
---------------------------------------------------------------------------------
CONFIG  (secrets come from environment variables — never hard-coded here)
  HIBOB_SERVICE_USER_ID      HiBob service user id     (Basic-auth username)
  HIBOB_SERVICE_USER_TOKEN   HiBob service user token  (Basic-auth password)
  Optional:
  HIBOB_BASE_URL             default 'https://api.hibob.com'
  SECUSYS_INBOX_DIR          folder the SFTP file lands in (if not passing --file)
  SECUSYS_ARCHIVE_DIR        folder to move processed files into

EMPLOYEE MAPPING
  Bob's attendance API identifies people by Bob's internal employee id. This
  script does NOT assume Secusys numbers equal any Bob id. Instead it calls
  /v1/people/search, reads each employee's id + employee-number field, and matches
  the Secusys number against that field (default: work.employeeIdInCompany). Point
  --bob-match-field at a different Bob field if the Secusys number lives elsewhere.

USAGE
  # Offline check: parse + pair + show entries, contact Bob for nothing.
  python3 secusys_to_hibob.py --file sample_extract.txt --dry-run

  # Validate mapping against Bob without writing (credentials set -> resolves ids):
  python3 secusys_to_hibob.py --file sample_extract.txt --dry-run

  # Nightly: newest file in the landing dir -> create entries -> archive it.
  python3 secusys_to_hibob.py --inbox /data/secusys/inbox --archive /data/secusys/done
=================================================================================
"""
from __future__ import annotations

import argparse
import base64
import datetime as dt
import glob
import json
import logging
import os
import shutil
import sys
import time
import urllib.error
import urllib.request

# --- Fixed-width column map (0-based; edit here if the extract format changes) --
RECORD_LEN = 30
EMP_SLICE = slice(0, 9)      # employee number, zero-padded
DATE_SLICE = slice(14, 22)   # DD-MM-YY
READER_SLICE = slice(22, 25) # reader/terminal id
TIME_SLICE = slice(25, 29)   # HHMM
DIR_INDEX = 29               # single char: B (entrance) / E (exit)
DIR_IN, DIR_OUT = "B", "E"

BATCH_SIZE = 100             # /attendance/entries accepts 1-100 items per request

log = logging.getLogger("secusys2hibob")


# --------------------------------------------------------------------------- #
#  Parsing
# --------------------------------------------------------------------------- #
class Punch:
    __slots__ = ("employee", "when", "direction", "reader", "raw")

    def __init__(self, employee, when, direction, reader, raw):
        self.employee = employee
        self.when = when
        self.direction = direction
        self.reader = reader
        self.raw = raw


def norm_number(value) -> str:
    """Normalise an employee number for matching: strip spaces and leading zeros."""
    s = str(value).strip().lstrip("0")
    return s or "0"


def parse_line(line: str):
    """Return a Punch, or None if the line should be skipped."""
    line = line.rstrip("\r\n")
    if not line.strip():
        return None
    if len(line) != RECORD_LEN:
        log.warning("skip malformed line (len=%d, expected %d): %r", len(line), RECORD_LEN, line)
        return None

    employee = line[EMP_SLICE].lstrip("0")
    if not employee:  # employee number 0 / blank -> unregistered card, can't map
        log.warning("skip punch with no employee number: %r", line)
        return None

    date_s = line[DATE_SLICE]      # DD-MM-YY
    time_s = line[TIME_SLICE]      # HHMM
    reader = line[READER_SLICE]
    direction = line[DIR_INDEX]

    if direction not in (DIR_IN, DIR_OUT):
        log.warning("skip punch with unknown direction %r: %r", direction, line)
        return None
    try:
        dd, mm, yy = date_s.split("-")
        when = dt.datetime(2000 + int(yy), int(mm), int(dd), int(time_s[:2]), int(time_s[2:]))
    except (ValueError, IndexError):
        log.warning("skip punch with bad date/time (%r %r): %r", date_s, time_s, line)
        return None

    return Punch(employee, when, direction, reader, line)


def read_punches(path: str):
    punches = []
    total = 0
    with open(path, "r", encoding="utf-8", errors="replace") as fh:
        for raw in fh:
            total += 1
            p = parse_line(raw)
            if p is not None:
                punches.append(p)
    log.info("read %s: %d lines, %d valid punches", path, total, len(punches))
    return punches


# --------------------------------------------------------------------------- #
#  Pairing entrance/exit into shifts
# --------------------------------------------------------------------------- #
class Shift:
    __slots__ = ("employee", "clock_in", "clock_out")

    def __init__(self, employee, clock_in, clock_out):
        self.employee = employee
        self.clock_in = clock_in
        self.clock_out = clock_out


def pair_shifts(punches, split_midnight: bool = True):
    """Pair each entrance (B) with the next exit (E) per employee, in time order."""
    by_emp = {}
    for p in punches:
        by_emp.setdefault(p.employee, []).append(p)

    shifts = []
    dropped_zero = orphan_exit = open_shift = 0

    for emp, plist in by_emp.items():
        plist.sort(key=lambda x: x.when)
        pending_in = None
        for p in plist:
            if p.direction == DIR_IN:
                if pending_in is not None:
                    open_shift += 1
                    log.warning("emp %s: entrance at %s with no matching exit before next entrance",
                                emp, pending_in.when)
                pending_in = p
            else:  # exit
                if pending_in is None:
                    orphan_exit += 1
                    log.warning("emp %s: exit at %s with no matching entrance", emp, p.when)
                    continue
                start, end = pending_in.when, p.when
                pending_in = None
                if end <= start:
                    dropped_zero += 1
                    log.warning("emp %s: dropping zero/negative shift %s -> %s", emp, start, end)
                    continue
                if start.date() == end.date():
                    shifts.append(Shift(emp, start, end))
                elif split_midnight:
                    # HiBob requires same-date entries: split at midnight boundaries.
                    cursor = start
                    while cursor.date() < end.date():
                        midnight = dt.datetime.combine(cursor.date() + dt.timedelta(days=1), dt.time.min)
                        seg_end = midnight - dt.timedelta(minutes=1)  # 23:59 same day
                        shifts.append(Shift(emp, cursor, seg_end))
                        cursor = midnight
                    shifts.append(Shift(emp, cursor, end))
                    log.info("emp %s: split overnight shift %s -> %s across midnight", emp, start, end)
                else:
                    open_shift += 1
                    log.warning("emp %s: skipping overnight shift %s -> %s", emp, start, end)
        if pending_in is not None:
            open_shift += 1
            log.warning("emp %s: entrance at %s never closed (no exit in file)", emp, pending_in.when)

    log.info("paired %d shift(s); dropped zero-length=%d, orphan exits=%d, unclosed/skipped=%d",
             len(shifts), dropped_zero, orphan_exit, open_shift)
    return shifts


# --------------------------------------------------------------------------- #
#  HiBob API helpers
# --------------------------------------------------------------------------- #
def _iso(t: dt.datetime) -> str:
    # ISO-8601 local time with seconds, e.g. "2026-08-12T09:13:00".
    return t.strftime("%Y-%m-%dT%H:%M:%S")


def _post(url: str, auth_header: str, payload: dict) -> dict:
    """POST JSON with Basic auth, retrying transient 429/5xx with backoff."""
    body = json.dumps(payload).encode("utf-8")
    for attempt in range(1, 6):
        req = urllib.request.Request(url, data=body, method="POST")
        req.add_header("Authorization", auth_header)
        req.add_header("Content-Type", "application/json")
        req.add_header("Accept", "application/json")
        try:
            with urllib.request.urlopen(req, timeout=60) as resp:
                return json.loads(resp.read().decode("utf-8") or "{}")
        except urllib.error.HTTPError as e:
            status = e.code
            detail = e.read().decode("utf-8", "replace")
            if attempt < 5 and (status == 429 or status >= 500):
                wait = 2 ** attempt
                log.warning("attempt %d failed HTTP %d; retry in %ds", attempt, status, wait)
                time.sleep(wait)
                continue
            raise RuntimeError(f"HiBob returned HTTP {status}: {detail}") from e
        except urllib.error.URLError as e:
            if attempt < 5:
                wait = 2 ** attempt
                log.warning("attempt %d network error (%s); retry in %ds", attempt, e.reason, wait)
                time.sleep(wait)
                continue
            raise
    raise RuntimeError("exhausted retries")


def _flatten(obj, out):
    """Collapse a nested / slash-keyed employee object to {last_segment: value}."""
    if isinstance(obj, dict):
        if set(obj.keys()) == {"value"}:            # {"value": x} wrapper
            _flatten(obj["value"], out)
            return
        for k, v in obj.items():
            seg = str(k).strip("/").replace(".", "/").split("/")[-1]
            if isinstance(v, (dict, list)):
                _flatten(v, out)
            else:
                out.setdefault(seg, v)
    elif isinstance(obj, list):
        for it in obj:
            _flatten(it, out)


def fetch_employee_map(base_url: str, auth_header: str, match_field: str,
                       include_inactive: bool) -> dict:
    """Return {normalised employee-number: Bob employee id} from /people/search."""
    url = f"{base_url}/v1/people/search"
    payload = {
        "fields": ["root.id", match_field],
        "showInactive": bool(include_inactive),
        "humanReadable": "REPLACE",
    }
    resp = _post(url, auth_header, payload)
    employees = resp.get("employees") if isinstance(resp, dict) else resp
    if not employees:
        raise RuntimeError("people/search returned no employees — check credentials/permissions.")

    match_key = match_field.replace("/", ".").split(".")[-1]   # e.g. employeeIdInCompany
    mapping = {}
    collisions = 0
    for emp in employees:
        flat = {}
        _flatten(emp, flat)
        bob_id = (emp.get("id") if isinstance(emp, dict) else None) or flat.get("id")
        number = flat.get(match_key)
        if not bob_id or number in (None, ""):
            continue
        key = norm_number(number)
        if key in mapping and mapping[key] != str(bob_id):
            collisions += 1
        mapping[key] = str(bob_id)
    log.info("people/search: %d employees, %d mapped by %s%s",
             len(employees), len(mapping), match_key,
             (" (%d duplicate numbers!)" % collisions) if collisions else "")
    return mapping


def build_items(shifts, emp_map, resolve: bool):
    """Turn shifts into /attendance/entries items. Returns (items, unmatched_numbers)."""
    items = []
    unmatched = []
    for s in shifts:
        if resolve:
            bob_id = emp_map.get(norm_number(s.employee))
            if not bob_id:
                unmatched.append(s.employee)
                continue
        else:
            bob_id = f"<lookup Secusys #{s.employee}>"   # dry-run without credentials
        items.append({
            "objectType": "attendanceEntry",
            "fields": {
                "/attendanceEntry/employeeId": {"value": bob_id},
                "/attendanceEntry/type": {"value": "work"},
                "/attendanceEntry/reportingMethod": {"value": "startEnd"},
                "/attendanceEntry/clockInTime": {"value": _iso(s.clock_in)},
                "/attendanceEntry/clockOutTime": {"value": _iso(s.clock_out)},
            },
        })
    return items, unmatched


def create_entries(base_url: str, auth_header: str, items, dry_run: bool) -> int:
    """POST items to /v1/attendance/entries in batches. Returns count of failures."""
    if not items:
        log.info("no entries to create")
        return 0
    url = f"{base_url}/v1/attendance/entries"
    created = failed = 0
    for i in range(0, len(items), BATCH_SIZE):
        batch = items[i:i + BATCH_SIZE]
        n = i // BATCH_SIZE + 1
        if dry_run:
            log.info("[DRY RUN] batch %d: %d entries\n%s", n, len(batch),
                     json.dumps({"items": batch}, indent=2))
            continue
        try:
            resp = _post(url, auth_header, {"items": batch})
            ids = resp.get("ids") if isinstance(resp, dict) else None
            got = len(ids) if isinstance(ids, list) else len(batch)
            created += got
            log.info("batch %d: created %d entr(ies)", n, got)
        except RuntimeError as e:
            failed += len(batch)
            log.error("batch %d FAILED: %s", n, e)
    if not dry_run:
        log.info("attendance entries done: created=%d failed=%d", created, failed)
    return failed


# --------------------------------------------------------------------------- #
#  File selection / archiving
# --------------------------------------------------------------------------- #
def pick_file(args) -> str:
    if args.file:
        return args.file
    inbox = args.inbox or os.environ.get("SECUSYS_INBOX_DIR")
    if not inbox:
        raise SystemExit("No input: pass --file, or --inbox / SECUSYS_INBOX_DIR.")
    candidates = [p for p in glob.glob(os.path.join(inbox, args.pattern)) if os.path.isfile(p)]
    if not candidates:
        raise SystemExit(f"No files matching {args.pattern!r} in {inbox}")
    newest = max(candidates, key=os.path.getmtime)
    log.info("selected newest file: %s", newest)
    return newest


def archive_file(path: str, args) -> None:
    archive = args.archive or os.environ.get("SECUSYS_ARCHIVE_DIR")
    if not archive or args.dry_run:
        return
    os.makedirs(archive, exist_ok=True)
    dest = os.path.join(archive, os.path.basename(path))
    shutil.move(path, dest)
    log.info("archived processed file -> %s", dest)


# --------------------------------------------------------------------------- #
#  Main
# --------------------------------------------------------------------------- #
def main(argv) -> int:
    ap = argparse.ArgumentParser(description="Load Secusys attendance extract into HiBob.")
    ap.add_argument("--file", help="specific extract file to process")
    ap.add_argument("--inbox", help="folder the SFTP file lands in (else SECUSYS_INBOX_DIR)")
    ap.add_argument("--archive", help="move processed files here (else SECUSYS_ARCHIVE_DIR)")
    ap.add_argument("--pattern", default="time_*.txt",
                    help="glob for files in --inbox (default: time_*.txt, matching "
                         "the Secusys extract name time_DD_MM_YYYY_HH.MM.SS.txt)")
    ap.add_argument("--bob-match-field", default="work.employeeIdInCompany",
                    help="Bob field (people/search dot notation) that holds the Secusys "
                         "employee number (default: work.employeeIdInCompany)")
    ap.add_argument("--include-inactive", action="store_true",
                    help="also map inactive/terminated employees from people/search")
    ap.add_argument("--base-url", default=os.environ.get("HIBOB_BASE_URL", "https://api.hibob.com"))
    ap.add_argument("--no-split-midnight", action="store_true",
                    help="skip overnight shifts instead of splitting them at midnight")
    ap.add_argument("--dry-run", action="store_true", help="parse and build entries, create nothing")
    ap.add_argument("--log-file", help="also write logs to this file")
    args = ap.parse_args(argv)

    handlers = [logging.StreamHandler(sys.stdout)]
    if args.log_file:
        handlers.append(logging.FileHandler(args.log_file))
    logging.basicConfig(level=logging.INFO, format="%(asctime)s [%(levelname)s] %(message)s",
                        handlers=handlers)

    user = os.environ.get("HIBOB_SERVICE_USER_ID")
    token = os.environ.get("HIBOB_SERVICE_USER_TOKEN")
    have_creds = bool(user and token)
    if not args.dry_run and not have_creds:
        raise SystemExit("Set HIBOB_SERVICE_USER_ID and HIBOB_SERVICE_USER_TOKEN (see README).")
    auth_header = ("Basic " + base64.b64encode(f"{user}:{token}".encode("ascii")).decode("ascii")
                  ) if have_creds else ""

    path = pick_file(args)
    punches = read_punches(path)
    shifts = pair_shifts(punches, split_midnight=not args.no_split_midnight)

    # Resolve Secusys numbers -> Bob employee ids via people/search (when we have creds).
    emp_map = {}
    if have_creds:
        emp_map = fetch_employee_map(args.base_url, auth_header, args.bob_match_field,
                                     args.include_inactive)
    else:
        log.warning("no credentials -> dry run only, employee ids NOT resolved against Bob")

    items, unmatched = build_items(shifts, emp_map, resolve=have_creds)
    if unmatched:
        uniq = sorted(set(unmatched), key=lambda x: int(x) if x.isdigit() else 0)
        log.warning("%d shift(s) skipped: no Bob employee matched Secusys number(s): %s",
                    len(unmatched), ", ".join(uniq))

    failed = create_entries(args.base_url, auth_header, items, args.dry_run)

    if failed == 0 and not unmatched:
        archive_file(path, args)
        return 0
    if failed == 0 and unmatched:
        log.warning("all created, but %d unmatched employee number(s) — leaving file in place.",
                    len(set(unmatched)))
        return 2
    log.error("%d entr(ies) failed to create; leaving file in place for re-run.", failed)
    return 1


if __name__ == "__main__":
    sys.exit(main(sys.argv[1:]))

#!/usr/bin/env python3
"""
Secusys -> HiBob (Bob) | Time & Attendance loader
=================================================================================
Secusys runs on-prem and drops a nightly fixed-width text extract (pushed over
SFTP ~02:00). This script picks up that file, pairs each entrance/exit punch into
a work shift, and POSTs the shifts to the HiBob Attendance Import API.

  Secusys server ──SFTP nightly──▶  landing dir  ──▶  this script  ──▶  HiBob API

It is pure standard-library Python 3.8+ — nothing to pip install. Run it from
cron (Linux) or Task Scheduler (Windows) shortly after the file lands.

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
  employee number is 0 are skipped and counted — they can't be mapped to Bob.

  If a future extract uses different column widths, adjust the *_SLICE constants
  below; nothing else needs to change.
---------------------------------------------------------------------------------
CONFIG  (secrets come from environment variables — never hard-coded here)
  HIBOB_SERVICE_USER_ID      HiBob service user id     (Basic-auth username)
  HIBOB_SERVICE_USER_TOKEN   HiBob service user token  (Basic-auth password)
  Optional:
  HIBOB_ID_TYPE              default 'idInCompany'  (or email / bobId / /path)
  HIBOB_IMPORT_METHOD        default 'aggregate'    (or 'immediate' for testing)
  HIBOB_BASE_URL             default 'https://api.hibob.com'
  SECUSYS_INBOX_DIR          folder the SFTP file lands in (if not passing --file)
  SECUSYS_ARCHIVE_DIR        folder to move processed files into

USAGE
  # Safe first run: parse + pair + print payloads, send nothing.
  python3 secusys_to_hibob.py --file sample_extract.txt --dry-run

  # Send one file immediately (records appear in Bob right away).
  python3 secusys_to_hibob.py --file /data/secusys/att_2026-08-12.txt --method immediate

  # Nightly: process the newest file in the landing dir, then archive it.
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

# We send timestamps in HiBob's documented DEFAULT datetime format: ISO 8601
# local time, e.g. "2026-08-12T09:13" (no timezone). Because it's the default,
# we omit the top-level `dateTimeFormat` field entirely (no pattern to mismatch).
BATCH_SIZE = 100                        # HiBob accepts ~100 entries per request

log = logging.getLogger("secusys2hibob")


# --------------------------------------------------------------------------- #
#  Parsing
# --------------------------------------------------------------------------- #
class Punch:
    __slots__ = ("employee", "when", "direction", "reader", "raw")

    def __init__(self, employee: str, when: dt.datetime, direction: str, reader: str, raw: str):
        self.employee = employee
        self.when = when
        self.direction = direction
        self.reader = reader
        self.raw = raw


def parse_line(line: str) -> Punch | None:
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


def read_punches(path: str) -> list[Punch]:
    punches: list[Punch] = []
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

    def __init__(self, employee: str, clock_in: dt.datetime, clock_out: dt.datetime):
        self.employee = employee
        self.clock_in = clock_in
        self.clock_out = clock_out


def pair_shifts(punches: list[Punch], split_midnight: bool = True) -> list[Shift]:
    """Pair each entrance (B) with the next exit (E) per employee, in time order."""
    by_emp: dict[str, list[Punch]] = {}
    for p in punches:
        by_emp.setdefault(p.employee, []).append(p)

    shifts: list[Shift] = []
    dropped_zero = orphan_exit = open_shift = 0

    for emp, plist in by_emp.items():
        plist.sort(key=lambda x: x.when)
        pending_in: Punch | None = None
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
#  HiBob API
# --------------------------------------------------------------------------- #
def _fmt(t: dt.datetime) -> str:
    # ISO 8601 local time, HiBob's default — e.g. "2026-08-12T09:13".
    return t.strftime("%Y-%m-%dT%H:%M")


def build_requests(shifts: list[Shift]) -> list[dict]:
    return [
        {
            "id": s.employee,
            "clockIn": _fmt(s.clock_in),
            "clockOut": _fmt(s.clock_out),
            "entryType": "work",
        }
        for s in shifts
    ]


def post_batch(base_url: str, method: str, id_type: str, auth_header: str, batch: list[dict]) -> dict:
    url = f"{base_url}/v1/attendance/import/{method}"
    body = json.dumps({
        "idType": id_type,
        "requests": batch,
    }).encode("utf-8")

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
                log.warning("batch attempt %d failed HTTP %d; retry in %ds", attempt, status, wait)
                time.sleep(wait)
                continue
            raise RuntimeError(f"HiBob returned HTTP {status}: {detail}") from e
        except urllib.error.URLError as e:
            if attempt < 5:
                wait = 2 ** attempt
                log.warning("batch attempt %d network error (%s); retry in %ds", attempt, e.reason, wait)
                time.sleep(wait)
                continue
            raise
    raise RuntimeError("exhausted retries")


def send_to_hibob(shifts: list[Shift], args, auth_header: str) -> int:
    requests = build_requests(shifts)
    if not requests:
        log.info("no shifts to send")
        return 0

    imported = failed = 0
    for i in range(0, len(requests), BATCH_SIZE):
        batch = requests[i:i + BATCH_SIZE]
        n = i // BATCH_SIZE + 1
        if args.dry_run:
            log.info("[DRY RUN] batch %d: %d entries\n%s", n, len(batch),
                     json.dumps({"idType": args.id_type, "requests": batch}, indent=2))
            continue
        resp = post_batch(args.base_url, args.method, args.id_type, auth_header, batch)
        imp = int(resp.get("imported", 0) or 0)
        notimp = int(resp.get("notImported", 0) or 0)
        imported += imp
        failed += notimp
        log.info("batch %d: status=%s imported=%d notImported=%d",
                 n, resp.get("status"), imp, notimp)
        for err in resp.get("errors", []) or []:
            log.warning("  hibob error: %s", json.dumps(err))
    if not args.dry_run:
        log.info("HiBob import done: imported=%d failed=%d", imported, failed)
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
def main(argv: list[str]) -> int:
    ap = argparse.ArgumentParser(description="Load Secusys attendance extract into HiBob.")
    ap.add_argument("--file", help="specific extract file to process")
    ap.add_argument("--inbox", help="folder the SFTP file lands in (else SECUSYS_INBOX_DIR)")
    ap.add_argument("--archive", help="move processed files here (else SECUSYS_ARCHIVE_DIR)")
    ap.add_argument("--pattern", default="time_*.txt",
                    help="glob for files in --inbox (default: time_*.txt, matching "
                         "the Secusys extract name time_DD_MM_YYYY_HH.MM.SS.txt)")
    ap.add_argument("--method", default=os.environ.get("HIBOB_IMPORT_METHOD", "aggregate"),
                    choices=["aggregate", "immediate"], help="HiBob import method")
    ap.add_argument("--id-type", default=os.environ.get("HIBOB_ID_TYPE", "idInCompany"),
                    help="HiBob field the employee number matches (idInCompany/email/bobId//path)")
    ap.add_argument("--base-url", default=os.environ.get("HIBOB_BASE_URL", "https://api.hibob.com"))
    ap.add_argument("--no-split-midnight", action="store_true",
                    help="skip overnight shifts instead of splitting them at midnight")
    ap.add_argument("--dry-run", action="store_true", help="parse and build payloads, send nothing")
    ap.add_argument("--log-file", help="also write logs to this file")
    args = ap.parse_args(argv)

    handlers = [logging.StreamHandler(sys.stdout)]
    if args.log_file:
        handlers.append(logging.FileHandler(args.log_file))
    logging.basicConfig(level=logging.INFO, format="%(asctime)s [%(levelname)s] %(message)s",
                        handlers=handlers)

    user = os.environ.get("HIBOB_SERVICE_USER_ID")
    token = os.environ.get("HIBOB_SERVICE_USER_TOKEN")
    if not args.dry_run and (not user or not token):
        raise SystemExit("Set HIBOB_SERVICE_USER_ID and HIBOB_SERVICE_USER_TOKEN (see README section 5).")
    auth_header = "Basic " + base64.b64encode(f"{user}:{token}".encode("ascii")).decode("ascii") \
        if user and token else ""

    path = pick_file(args)
    punches = read_punches(path)
    shifts = pair_shifts(punches, split_midnight=not args.no_split_midnight)
    failed = send_to_hibob(shifts, args, auth_header)

    if failed == 0:
        archive_file(path, args)
        return 0
    log.error("%d entr(ies) failed to import; leaving file in place for re-run.", failed)
    return 1


if __name__ == "__main__":
    sys.exit(main(sys.argv[1:]))

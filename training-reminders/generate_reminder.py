#!/usr/bin/env python3
"""Generate the WhatsApp reminder text for a given day's training session.

Usage:
    python3 generate_reminder.py                # tomorrow's session (or nothing)
    python3 generate_reminder.py 2026-09-07      # a specific date
    python3 generate_reminder.py --for-date 2026-09-07 --carpool-url https://...

Exit code is 0 and prints nothing to stdout when there is no session on that
date (so it's safe to wire into a cron job / scheduled check without extra
guarding), unless --quiet is not passed and you want an explicit message.
"""
import argparse
import datetime
import json
import pathlib
import sys

SCHEDULE_PATH = pathlib.Path(__file__).parent / "schedule.json"


def load_schedule():
    with open(SCHEDULE_PATH, encoding="utf-8") as f:
        return json.load(f)


def find_session(schedule, date_str):
    for session in schedule["sessions"]:
        if session["date"] == date_str:
            return session
    return None


def render_message(session, carpool_url=None):
    lines = ["תזכורת לאימון מחר \U0001F3C3"]
    lines.append(f'{session["activity"]} | {session["time"] or session.get("note") or "ראו פרטים בקבוצה"}')
    if session.get("location"):
        lines.append(f'מיקום: {session["location"]}')
    if session.get("coach"):
        lines.append(f'מאמן/ת: {session["coach"]}')
    if session.get("note") and session.get("time"):
        lines.append(session["note"])
    if carpool_url:
        lines.append("")
        lines.append(f"מי לוקח / מחזיר? עדכנו כאן: {carpool_url}")
    return "\n".join(lines)


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument(
        "date", nargs="?", default=None,
        help="Date to check (YYYY-MM-DD). Defaults to tomorrow.",
    )
    parser.add_argument("--carpool-url", default=None, help="Link to the carpool sign-up page.")
    parser.add_argument(
        "--say-nothing-scheduled", action="store_true",
        help="Print a short note when there's no session that day (default: print nothing).",
    )
    args = parser.parse_args()

    target = args.date or (datetime.date.today() + datetime.timedelta(days=1)).isoformat()

    schedule = load_schedule()
    session = find_session(schedule, target)

    if session is None:
        if args.say_nothing_scheduled:
            print(f"אין אימון מתוכנן בתאריך {target}.")
        return 0

    print(render_message(session, carpool_url=args.carpool_url))
    return 0


if __name__ == "__main__":
    sys.exit(main())

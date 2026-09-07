// Builds the WhatsApp reminder text for a given date from ../schedule.json.
// Kept in sync by hand with generate_reminder.py — same source data, same message shape.
const fs = require('fs');
const path = require('path');

const SCHEDULE_PATH = path.join(__dirname, '..', 'schedule.json');

function loadSchedule() {
  return JSON.parse(fs.readFileSync(SCHEDULE_PATH, 'utf8'));
}

function findSession(schedule, dateStr) {
  return schedule.sessions.find((s) => s.date === dateStr) || null;
}

function tomorrowISO() {
  const d = new Date();
  d.setDate(d.getDate() + 1);
  const pad = (n) => String(n).padStart(2, '0');
  return `${d.getFullYear()}-${pad(d.getMonth() + 1)}-${pad(d.getDate())}`;
}

function renderMessage(session, carpoolUrl) {
  const lines = ['תזכורת לאימון מחר \u{1F3C3}'];
  lines.push(`${session.activity} | ${session.time || session.note || 'אימון עצמאי'}`);
  if (session.location) lines.push(`מיקום: ${session.location}`);
  if (session.coach) lines.push(`מאמן/ת: ${session.coach}`);
  if (session.note && session.time) lines.push(session.note);
  if (carpoolUrl) {
    lines.push('');
    lines.push(`מי לוקח / מחזיר? עדכנו כאן: ${carpoolUrl}`);
  }
  return lines.join('\n');
}

// Returns null when there's no session on that date — callers should send nothing.
function reminderFor(dateStr, carpoolUrl) {
  const schedule = loadSchedule();
  const session = findSession(schedule, dateStr);
  if (!session) return null;
  return renderMessage(session, carpoolUrl);
}

module.exports = { loadSchedule, findSession, tomorrowISO, renderMessage, reminderFor };

// The nightly job: if there's a training session tomorrow, send the reminder straight into the
// WhatsApp group. Meant to run once a day via cron (e.g. 20:00) — it connects, sends (or does
// nothing when there's no session tomorrow), and exits; it does not stay running in between.
const fs = require('fs');
const path = require('path');
const { connect } = require('./connection');
const { reminderFor, tomorrowISO } = require('./reminder');

const CONFIG_PATH = path.join(__dirname, 'config.json');

function loadConfig() {
  if (!fs.existsSync(CONFIG_PATH)) {
    throw new Error('config.json חסר. העתיקו את config.example.json ל-config.json ומלאו groupJid (ראו list-groups.js).');
  }
  const config = JSON.parse(fs.readFileSync(CONFIG_PATH, 'utf8'));
  if (!config.groupJid || config.groupJid.includes('123456789012345678')) {
    throw new Error('config.json: groupJid לא הוגדר. הריצו node list-groups.js כדי למצוא את ה-JID הנכון.');
  }
  return config;
}

(async () => {
  const config = loadConfig();
  const date = process.argv[2] || tomorrowISO();
  const message = reminderFor(date, config.carpoolUrl);

  if (!message) {
    console.log(`אין אימון בתאריך ${date} — לא נשלחה הודעה.`);
    process.exit(0);
  }

  const sock = await connect();
  await sock.sendMessage(config.groupJid, { text: message });
  console.log(`נשלחה תזכורת לקבוצה עבור ${date}:\n\n${message}`);

  // give the socket a moment to flush the outgoing message before the process exits
  setTimeout(() => process.exit(0), 2000);
})().catch((err) => {
  console.error('שגיאה בשליחת התזכורת:', err.message || err);
  process.exit(1);
});

// One-time helper: connects, then prints every WhatsApp group this linked account belongs to,
// with its JID — copy the right one into config.json's "groupJid".
const { connect } = require('./connection');

(async () => {
  const sock = await connect();
  const groups = await sock.groupFetchAllParticipating();
  const list = Object.values(groups);

  if (list.length === 0) {
    console.log('לא נמצאו קבוצות. ודאו שהמספר המקושר הוא חבר בקבוצת ההורים.');
  } else {
    console.log('\nקבוצות שנמצאו:\n');
    for (const g of list) {
      console.log(`${g.subject}\n  ${g.id}\n`);
    }
    console.log('העתיקו את ה-JID (השורה שמסתיימת ב-@g.us) של קבוצת ההורים אל config.json תחת "groupJid".');
  }

  process.exit(0);
})().catch((err) => {
  console.error('שגיאה:', err.message || err);
  process.exit(1);
});

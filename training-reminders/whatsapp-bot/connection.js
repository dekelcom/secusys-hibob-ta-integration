// Shared WhatsApp connection helper, built on Baileys (unofficial, linked-device protocol —
// the same mechanism WhatsApp Web/Desktop use). First run prints a QR code to scan once from
// the phone that's a member of the target group (Settings > Linked devices > Link a device);
// the session is then cached in ./auth_info so later runs reconnect without a new scan.
const path = require('path');
const pino = require('pino');
const qrcode = require('qrcode-terminal');
const { default: makeWASocket, useMultiFileAuthState, DisconnectReason } = require('@whiskeysockets/baileys');

const AUTH_DIR = path.join(__dirname, 'auth_info');

// Resolves once the socket is fully connected ("open"). Rejects if the session was logged out
// (auth_info deleted or unlinked from the phone) — delete auth_info and re-scan in that case.
async function connect() {
  const { state, saveCreds } = await useMultiFileAuthState(AUTH_DIR);
  const sock = makeWASocket({
    auth: state,
    logger: pino({ level: 'silent' }),
  });
  sock.ev.on('creds.update', saveCreds);

  return new Promise((resolve, reject) => {
    sock.ev.on('connection.update', (update) => {
      const { connection, lastDisconnect, qr } = update;
      if (qr) {
        console.log('\nסרקו את הקוד הזה מהוואטסאפ בטלפון (הגדרות > מכשירים מקושרים > קישור מכשיר):\n');
        qrcode.generate(qr, { small: true });
      }
      if (connection === 'open') {
        resolve(sock);
      } else if (connection === 'close') {
        const statusCode = lastDisconnect?.error?.output?.statusCode;
        if (statusCode === DisconnectReason.loggedOut) {
          reject(new Error('החיבור נותק (Logged out). מחקו את תיקיית auth_info וסרקו קוד QR מחדש.'));
        } else {
          reject(new Error('החיבור לוואטסאפ נסגר לפני שהצליח להתחבר. נסו שוב.'));
        }
      }
    });
  });
}

module.exports = { connect };

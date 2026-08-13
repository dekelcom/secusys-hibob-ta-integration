# Secusys → HiBob (Bob) — Attendance sync

This package pushes daily **clock-in / clock-out data from Secusys into HiBob**,
so attendance appears in Bob automatically for approval and payroll — no manual
typing.

It reads the nightly text file that Secusys already exports (the one sent by SFTP
around 02:00), turns each entrance/exit into a shift, and sends it to HiBob using
Bob's official Attendance Import API.

**You only need to do the setup once.** After that it runs by itself every night.

---

## What's in this folder

| File | What it is |
|------|------------|
| `secusys_to_hibob.py` | The program. You don't edit it. |
| `sample_extract.txt` | An example Secusys file, for a safe first test. |
| `README.md` | This guide. |

---

## Before you start — you need 3 things

1. **Python 3.8 or newer** on the computer that receives the nightly Secusys file.
   Check with: `python3 --version` (Windows: `python --version`). Nothing else to install.
2. **A HiBob service user.** In Bob go to **Settings → Integrations → Service users
   → New service user**, give it permission to **import attendance**, and copy its
   **Service User ID** and **Token**.
3. **The employee number match.** The number Secusys uses for each employee must equal
   that employee's **Employee ID** field in Bob. (If you match on something else —
   email or ID number — see step 6.)

---

## Setup — step by step

### Step 1 — Put the files on the server
Copy this whole folder onto the computer that receives the nightly Secusys file,
e.g. `C:\secusys-hibob\` (Windows) or `/opt/secusys-hibob/` (Linux).

### Step 2 — Enter the HiBob credentials
Set two values so the program can log in to Bob (they are never written into the files).

**Windows (PowerShell):**
```powershell
setx HIBOB_SERVICE_USER_ID "your-service-user-id"
setx HIBOB_SERVICE_USER_TOKEN "your-service-user-token"
```
**Linux/Mac:**
```bash
export HIBOB_SERVICE_USER_ID="your-service-user-id"
export HIBOB_SERVICE_USER_TOKEN="your-service-user-token"
```

### Step 3 — Do a dry run (sends nothing)
This proves the program reads the file correctly. It only prints — it does **not**
touch Bob.
```
python3 secusys_to_hibob.py --file sample_extract.txt --dry-run
```
You should see a clean list of shifts (e.g. employee `368` → `09:13`–`18:54`).

### Step 4 — Send one real day as a test
Pick a real Secusys file and send it **immediately** so it shows up in Bob at once:
```
python3 secusys_to_hibob.py --file time_13_08_2026_02.15.02.txt --method immediate
```
Then open an employee in Bob → **Attendance** and confirm the shift is there. ✅

### Step 5 — Point it at the nightly folder and schedule it
Tell it the folder the SFTP file lands in and where to move files once done:
```
python3 secusys_to_hibob.py --inbox /path/to/incoming --archive /path/to/done
```
It automatically picks the newest `time_*.txt` file.

Now schedule that command to run every night at **02:30** (after the 02:00 file arrives):

- **Windows:** Task Scheduler → Create Task → Trigger: Daily 02:30 →
  Action: Program `python`, Arguments:
  `"C:\secusys-hibob\secusys_to_hibob.py" --inbox "D:\incoming" --archive "D:\done"`
- **Linux:** add to `crontab -e`:
  ```
  30 2 * * *  /usr/bin/python3 /opt/secusys-hibob/secusys_to_hibob.py --inbox /path/incoming --archive /path/done --log-file /var/log/secusys-hibob.log
  ```

That's it. Attendance now flows into Bob automatically every morning.

---

## Step 6 — (Only if needed) match employees a different way

By default the Secusys employee number is matched to the **Employee ID** in Bob.
To match on something else, add `--id-type`:

| Match on | Add this |
|----------|----------|
| Employee ID in Bob *(default)* | *(nothing)* |
| Work email | `--id-type email` |
| ID number / other custom field in Bob | `--id-type /identification/custom/<field-id>` |

---

## If something looks wrong

| You see | What it means / what to do |
|---------|----------------------------|
| `Set HIBOB_SERVICE_USER_ID ...` | The two credentials from Step 2 aren't set for this user. |
| `No files matching time_*.txt` | Wrong `--inbox` folder, or the file name is different — check where SFTP drops it. |
| Many "skip malformed line" | The Secusys file format changed. Send us one file and we'll adjust. |
| `notImported` greater than 0, "employee not found" | The employee number doesn't match Bob's Employee ID — see Step 6. |
| `HTTP 401` | Wrong Service User ID/Token, or it lacks attendance-import permission. |
| A run fails | The file is left in place and the next run retries it. Logs are printed (and saved if you used `--log-file`). |

---

## What it sends to HiBob (for reference)

It calls Bob's official endpoint
`POST https://api.hibob.com/v1/attendance/import/{immediate|aggregate}`
(Basic auth with the service user), with this body — exactly per HiBob's API:

```json
{
  "idType": "idInCompany",
  "requests": [
    { "id": "368", "clockIn": "2026-08-12T09:13", "clockOut": "2026-08-12T18:54", "entryType": "work" }
  ]
}
```

It automatically obeys Bob's rules: same-date entries (overnight shifts are split at
midnight), no overlapping or future entries, and batches of up to 100.

---

## Appendix — the Secusys file format it reads

Each line is a single punch, 30 characters, e.g. `0000003680000012-08-260010913B`:

| Position | Meaning | Example |
|----------|---------|---------|
| 1–9   | Employee number | `000000368` → 368 |
| 10–14 | (filler) | `00000` |
| 15–22 | Date `DD-MM-YY` | `12-08-26` → 12 Aug 2026 |
| 23–25 | Reader/terminal | `001` |
| 26–29 | Time `HHMM` | `0913` → 09:13 |
| 30    | `B` = entrance, `E` = exit | `B` |

Lines with no employee number, or an entrance/exit at the same minute, are skipped
automatically. If a site's format differs, the column positions are the `*_SLICE`
values at the top of `secusys_to_hibob.py`.

# Secusys → HiBob (Bob) — Attendance sync

This package pushes daily **clock-in / clock-out data from Secusys into HiBob**,
so attendance appears in Bob automatically for approval and payroll — no manual
typing.

It reads the nightly text file that Secusys already exports (the one sent by SFTP
around 02:00), turns each entrance/exit into a shift, looks up each employee in
Bob, and creates the attendance entries using Bob's official APIs:

- **`POST /v1/people/search`** — to match each Secusys employee number to the
  employee's Bob record.
- **`POST /v1/attendance/entries`** — to create the clock-in/out entries.

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
   → New service user**, and give it permission to **read employees** (People) and to
   **create/manage attendance**. Copy its **Service User ID** and **Token**.
3. **The employee number match.** The number Secusys uses for each employee must equal
   that employee's **Employee ID** (`employeeIdInCompany`) in Bob. If the Secusys number
   lives in a different Bob field, see step 6.

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

### Step 3 — Dry run (creates nothing)
The dry run never writes to Bob. Run it two ways:

**a) Offline** — just checks the file is read correctly:
```
python3 secusys_to_hibob.py --file sample_extract.txt --dry-run
```
You'll see the shifts (e.g. employee `368` → `09:13`–`18:54`) with a
`<lookup Secusys #…>` placeholder where the Bob employee id will go.

**b) With your credentials set** — it additionally calls Bob's People Search to
**check the mapping**, and lists any Secusys numbers that don't match a Bob employee
— all **without creating anything**:
```
python3 secusys_to_hibob.py --file time_13_08_2026_02.15.02.txt --dry-run
```

### Step 4 — Create one real day as a test
Run it on a real file (no `--dry-run`). It looks employees up in Bob and creates
the entries:
```
python3 secusys_to_hibob.py --file time_13_08_2026_02.15.02.txt
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

## Step 6 — (Only if needed) match employees on a different Bob field

By default the Secusys number is matched to Bob's **Employee ID**
(`work.employeeIdInCompany`). If the Secusys number is stored in a different Bob
field, point the matcher at it (People Search dot-notation field id):

```
--bob-match-field /custom/table/1234567890   # example custom field
```

Add `--include-inactive` if you also need to match employees who are marked
inactive/terminated in Bob.

---

## If something looks wrong

| You see | What it means / what to do |
|---------|----------------------------|
| `Set HIBOB_SERVICE_USER_ID ...` | The two credentials from Step 2 aren't set for this user. |
| `no Bob employee matched Secusys number(s): …` | Those employees exist in Secusys but their number isn't on any Bob record — fix the Employee ID in Bob, or use Step 6. Those shifts are skipped; the rest still load. |
| `No files matching time_*.txt` | Wrong `--inbox` folder, or the file name is different — check where SFTP drops it. |
| Many "skip malformed line" | The Secusys file format changed. Send us one file and we'll adjust. |
| `people/search returned no employees` | The service user lacks People read permission, or the token is wrong. |
| `HTTP 401` | Wrong Service User ID/Token, or it lacks the required permissions. |
| A run fails | The file is left in place and the next run retries it. Logs are printed (and saved if you used `--log-file`). |

---

## What it sends to HiBob (for reference)

**1. Look up employees** (once per run) to map the Secusys number to the Bob id:
```
POST https://api.hibob.com/v1/people/search
{
  "fields": ["root.id", "work.employeeIdInCompany"],
  "showInactive": false,
  "humanReadable": "REPLACE"
}
```

**2. Create the attendance entries** in batches of up to 100:
```
POST https://api.hibob.com/v1/attendance/entries
{
  "items": [
    {
      "objectType": "attendanceEntry",
      "fields": {
        "/attendanceEntry/employeeId":      { "value": "3308236575481004065" },
        "/attendanceEntry/type":            { "value": "work" },
        "/attendanceEntry/reportingMethod": { "value": "startEnd" },
        "/attendanceEntry/clockInTime":     { "value": "2026-08-12T09:13:00" },
        "/attendanceEntry/clockOutTime":    { "value": "2026-08-12T18:54:00" }
      }
    }
  ]
}
```
Both use Basic auth with the service user. Timestamps are ISO-8601 local time, and
clock-in/clock-out are always on the same date (overnight shifts are split at
midnight automatically). Zero-length and no-employee punches are skipped.

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

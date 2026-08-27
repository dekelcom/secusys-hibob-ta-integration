/* =============================================================================
   Secusys  ->  attendance export  (produces the nightly time_*.txt file)
   -----------------------------------------------------------------------------
   Run this on the Secusys SQL Server. For every employee, for every day in the
   range, it outputs TWO fixed-width records:

       * the FIRST punch of the day  -> entrance  (B)
       * the LAST  punch of the day  -> exit      (E)

   i.e. the classic "first in / last out" shift. The output matches, byte for
   byte, the 30-character layout that secusys_to_hibob.py expects:

       0000003680000012-08-260010913B
       |-- emp --|filler|-date--|rdr|tm|dir

       offset  width  field
       0-8     9      employee number (zero-padded)
       9-13    5      filler  (00000)
       14-21   8      date    DD-MM-YY
       22-24   3      reader / terminal id  (fixed 001 here)
       25-28   4      time    HHMM
       29      1      direction  B=first/in, E=last/out

   HOW TO INSTALL
     1. Replace every {{PLACEHOLDER}} below with the real Secusys table/columns
        (see the three you must set, just under RawPunches).
     2. Run it once in SSMS and eyeball the output.
     3. Schedule it to write the file every night — see run_secusys_export.bat
        (or the sqlcmd one-liner in the README).

   NOTES / ASSUMPTIONS
     - "First in / last out": the earliest punch of the day is emitted as the
       entrance and the latest as the exit, regardless of the raw reader
       direction. Days with only a single punch are skipped (no shift).
     - Output is one text column named `Line`; run with sqlcmd -h -1 -W to get a
       clean file with no header and no trailing spaces.
   ============================================================================= */
SET NOCOUNT ON;

/* ------------------------- CONFIG: date range ----------------------------- */
DECLARE @FromDate date = CAST(DATEADD(day, -1, GETDATE()) AS date);  -- yesterday
DECLARE @ToDate   date = CAST(DATEADD(day, -1, GETDATE()) AS date);  -- yesterday
/* -------------------------------------------------------------------------- */

;WITH
/* 1) Point this at the real Secusys punches table. These are the ONLY three
      things you normally change per install:
        {{EMPLOYEE_ID_COLUMN}}    -> the badge/employee number
        {{EVENT_DATETIME_COLUMN}} -> the punch timestamp (datetime)
        {{PUNCHES_TABLE}}         -> the raw events/punches table            */
RawPunches AS (
    SELECT
        p.{{EMPLOYEE_ID_COLUMN}}    AS EmployeeId,
        p.{{EVENT_DATETIME_COLUMN}} AS EventTime
    FROM {{PUNCHES_TABLE}} AS p        -- e.g.  dbo.tblEvents  /  dbo.AttendanceLog
    WHERE p.{{EVENT_DATETIME_COLUMN}} >= @FromDate
      AND p.{{EVENT_DATETIME_COLUMN}} <  DATEADD(day, 1, @ToDate)
      /* Optional: keep only real T&A readers, drop door-access-only punches:
         AND p.{{READER_COLUMN}} IN ({{TIME_ATTENDANCE_READER_IDS}})          */
),

/* 2) First and last punch per employee per day. */
DayBounds AS (
    SELECT
        EmployeeId,
        CAST(EventTime AS date) AS WorkDate,
        MIN(EventTime)          AS FirstPunch,
        MAX(EventTime)          AS LastPunch
    FROM RawPunches
    WHERE EmployeeId IS NOT NULL
    GROUP BY EmployeeId, CAST(EventTime AS date)
    HAVING MIN(EventTime) < MAX(EventTime)     -- need at least two distinct punches
),

/* 3) Explode into one entrance (B) and one exit (E) record. */
Records AS (
    SELECT EmployeeId, FirstPunch AS PunchTime, 'B' AS Direction FROM DayBounds
    UNION ALL
    SELECT EmployeeId, LastPunch  AS PunchTime, 'E' AS Direction FROM DayBounds
)

/* 4) Format each record as the exact 30-character line. */
SELECT
      RIGHT('000000000' + CAST(EmployeeId AS varchar(9)), 9)     -- [0-8]  employee, padded
    + '00000'                                                    -- [9-13] filler
    + CONVERT(varchar(8), PunchTime, 5)                          -- [14-21] DD-MM-YY
    + '001'                                                      -- [22-24] reader/terminal
    + REPLACE(CONVERT(varchar(5), PunchTime, 108), ':', '')      -- [25-28] HHMM
    + Direction                                                  AS Line     -- [29] B / E
FROM Records
ORDER BY EmployeeId, PunchTime;

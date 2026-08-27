@echo off
REM ===========================================================================
REM  Secusys nightly attendance export
REM  Runs secusys_export.sql and writes the fixed-width file that
REM  secusys_to_hibob.py reads: time_DD_MM_YYYY_HH.MM.SS.txt
REM
REM  EDIT the 3 settings below, then schedule this .bat nightly (~02:00) in
REM  Windows Task Scheduler. For SQL logins instead of Windows auth, replace
REM  -E with:  -U your_user -P your_password
REM ===========================================================================

set "SERVER=YOUR_SQL_SERVER"
set "DATABASE=Secusys"
set "OUTDIR=C:\secusys\outbox"

set "SCRIPT=%~dp0secusys_export.sql"

REM Locale-independent timestamp -> dd_MM_yyyy_HH.mm.ss
for /f %%T in ('powershell -NoProfile -Command "Get-Date -Format \"dd_MM_yyyy_HH.mm.ss\""') do set "STAMP=%%T"
set "OUTFILE=%OUTDIR%\time_%STAMP%.txt"

if not exist "%OUTDIR%" mkdir "%OUTDIR%"

REM  -h -1  no header row     -W  trim trailing spaces     -o  output file
sqlcmd -S %SERVER% -d %DATABASE% -E -i "%SCRIPT%" -h -1 -W -o "%OUTFILE%"

if errorlevel 1 (
    echo ERROR: sqlcmd failed. Check SERVER/DATABASE/credentials and secusys_export.sql.
    exit /b 1
)
echo Wrote %OUTFILE%

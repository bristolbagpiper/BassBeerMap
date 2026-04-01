@echo off
setlocal

cd /d "%~dp0"

for /f %%I in ('powershell -NoProfile -Command "(Get-ChildItem -LiteralPath '.' -Filter '*.pdf' | Measure-Object).Count"') do set PDF_COUNT=%%I

if "%PDF_COUNT%"=="0" (
  echo No PDF file was found in this folder.
  echo Put exactly one PDF in:
  echo %~dp0
  pause
  exit /b 1
)

if not "%PDF_COUNT%"=="1" (
  echo More than one PDF file was found in this folder.
  echo Leave exactly one PDF in:
  echo %~dp0
  pause
  exit /b 1
)

for /f "delims=" %%I in ('powershell -NoProfile -Command "(Get-ChildItem -LiteralPath '.' -Filter '*.pdf').Name"') do set PDF_NAME=%%I

echo Converting "%PDF_NAME%" to pubs.csv and directory-meta.json...
python convert_pdf_to_csv.py "%PDF_NAME%" "pubs.csv"

if errorlevel 1 (
  echo.
  echo Conversion failed.
  pause
  exit /b 1
)

echo.
echo pubs.csv and directory-meta.json have been updated.
pause

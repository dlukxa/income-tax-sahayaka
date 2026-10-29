@echo off
setlocal
cd /d "%~dp0"
where py >nul 2>nul
if errorlevel 1 (
  echo Python launcher was not found. Install Python 3.10+ first.
  pause
  exit /b 1
)
if not exist .venv py -3 -m venv .venv
call .venv\Scripts\activate.bat
python -m pip install -r requirements.txt
if errorlevel 1 (
  echo Could not install dependencies. Check your internet connection.
  pause
  exit /b 1
)
if "%FLASK_SECRET_KEY%"=="" set FLASK_SECRET_KEY=replace-before-sharing-with-a-random-secret
if "%REVIEWER_ACCESS_CODE%"=="" set REVIEWER_ACCESS_CODE=review-demo-change-me
if "%TAX_EXPERT_ACCESS_CODE%"=="" set TAX_EXPERT_ACCESS_CODE=tax-expert-demo-change-me
if "%ADMIN_ACCESS_CODE%"=="" set ADMIN_ACCESS_CODE=admin-demo-change-me
python app.py
pause

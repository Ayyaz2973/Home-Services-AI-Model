@echo off
cd /d %~dp0
if not exist venv\Scripts\activate.bat (
  echo venv not found. Run setup.bat first.
  pause
  exit /b
)
call venv\Scripts\activate
if not exist static mkdir static
copy /Y index.html static\index.html >nul
copy /Y tech.html static\tech.html >nul
copy /Y admin.html static\admin.html >nul
copy /Y terms.html static\terms.html >nul
copy /Y style.css static\style.css >nul
copy /Y common.js static\common.js >nul
set ADMIN_KEY=change-me-now
set OLLAMA_MODEL=qwen2.5:14b
set COMMISSION_PCT=10
set ASSIGN_TIMEOUT_MIN=10
start "" cmd /c "timeout /t 4 >nul & start http://localhost:8000"
uvicorn main:app --host 0.0.0.0 --port 8000
pause
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
copy /Y favicon.svg static\favicon.svg >nul
copy /Y style.css static\style.css >nul
copy /Y common.js static\common.js >nul
set ADMIN_KEY=Kayani_73
if exist .env (
  for /f "usebackq tokens=1,2 delims==" %%A in (".env") do (
    if "%%A"=="GEMINI_API_KEY" if "%GEMINI_API_KEY%"=="" set GEMINI_API_KEY=%%B
    if "%%A"=="GEMINI_MODEL" if "%GEMINI_MODEL%"=="" set GEMINI_MODEL=%%B
  )
)
rem Free Gemini API key from https://aistudio.google.com/app/apikey
if "%GEMINI_API_KEY%"=="" set GEMINI_API_KEY=
if "%GEMINI_MODEL%"=="" set GEMINI_MODEL=gemini-3.1-flash-lite
set OLLAMA_MODEL=qwen2.5:14b
set COMMISSION_PCT=10
set ASSIGN_TIMEOUT_MIN=10
start "" cmd /c "timeout /t 4 >nul & start http://localhost:8000"
uvicorn main:app --host 0.0.0.0 --port 8000
pause
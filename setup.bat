@echo off
cd /d %~dp0
python -m venv venv
call venv\Scripts\activate
python -m pip install --upgrade pip
pip install -r requirements.txt
echo.
echo Setup done. Now run: ollama pull qwen2.5:14b
echo Then double-click run.bat
pause

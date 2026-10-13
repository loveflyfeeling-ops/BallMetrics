@echo off
chcp 65001 >nul
cd /d %~dp0
pip install -r requirements.txt -q
start "" http://127.0.0.1:8000
python app.py
pause

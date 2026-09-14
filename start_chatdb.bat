@echo off
chcp 65001 >nul
cd /d "%~dp0"
echo ========================================
echo ChatGPT 本地聊天数据库服务
echo API: http://127.0.0.1:17891
echo DB : %~dp0chatgpt_history.sqlite3
echo ========================================
python chatdb.py serve
pause

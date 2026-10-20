@echo off
rem SerialLens Web 智能体一键启动（端口 8300）
rem 访问密码来源（优先级）：环境变量 SERIALLENS_ACCESS_PASSWORD > webapp 目录下的本地密码文件 > 自动生成
rem SERIALLENS_ACCESS_PASSWORD 已在系统环境变量中固定设置
cd /d %~dp0
set PYTHONUTF8=1
python -m uvicorn webapp.app:app --host 0.0.0.0 --port 8300
pause

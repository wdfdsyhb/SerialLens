@echo off
rem SerialLens Web 智能体一键启动（端口 8300）
rem
rem 访问密码来源（优先级）：
rem   1. 环境变量 SERIALLENS_ACCESS_PASSWORD
rem   2. webapp\.env.local 本地密码文件
rem   3. 自动生成（启动时打印一次）
rem
rem 绑定地址（默认仅本机 127.0.0.1，局域网演示才需要改）：
rem   set SERIALLENS_BIND_HOST=0.0.0.0
rem 改之前请确认密码足够强：0.0.0.0 意味着同网段任何人都能访问登录页。
cd /d %~dp0
set PYTHONUTF8=1
if "%SERIALLENS_BIND_HOST%"=="" set SERIALLENS_BIND_HOST=127.0.0.1
echo [SerialLens] 绑定地址: %SERIALLENS_BIND_HOST%:8300
python -m uvicorn webapp.app:app --host %SERIALLENS_BIND_HOST% --port 8300
pause

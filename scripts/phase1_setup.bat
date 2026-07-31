@echo off
chcp 65001 >nul
setlocal enabledelayedexpansion

REM ============================================================
REM  Zampto Phase 1 - Windows One-Click Setup Script
REM  - Clones repo (or pulls if exists)
REM  - Creates venv
REM  - Installs deps
REM  - Prompts for credentials
REM  - Runs interactive login -> saves session.json
REM  - Prints base64 string for GitHub Secret
REM ============================================================

title Zampto Phase 1 - 本地登录工具

echo.
echo ============================================================
echo   Zampto Phase 1 - Windows 一键登录脚本
echo ============================================================
echo.

REM ---- 0. Check Python ----
where python >nul 2>&1
if errorlevel 1 (
    echo [ERROR] 未检测到 Python，请先安装 Python 3.10+:
    echo         https://www.python.org/downloads/windows/
    echo         安装时务必勾选 "Add Python to PATH"
    echo.
    pause
    exit /b 1
)

echo [OK] 已检测到 Python:
python --version
echo.

REM ---- 1. Choose working directory ----
set "WORKDIR=%USERPROFILE%\zampto"
if exist "%WORKDIR%" (
    echo [INFO] 检测到已存在目录 %WORKDIR%
    set /p "OVERWRITE=是否删除并重新克隆? (y/N): "
    if /i "!OVERWRITE!"=="y" (
        rmdir /s /q "%WORKDIR%"
        echo [INFO] 正在重新克隆仓库...
        git clone https://github.com/weikkadd/zampto.git "%WORKDIR%"
    ) else (
        echo [INFO] 使用现有目录，尝试拉取最新代码...
        cd /d "%WORKDIR%"
        git pull origin main
    )
) else (
    echo [INFO] 正在克隆仓库到 %WORKDIR% ...
    git clone https://github.com/weikkadd/zampto.git "%WORKDIR%"
)
cd /d "%WORKDIR%"
if errorlevel 1 (
    echo [ERROR] 克隆失败，请检查网络或 git 是否已安装
    pause
    exit /b 1
)
echo [OK] 仓库就绪
echo.

REM ---- 2. Create venv ----
if not exist "venv" (
    echo [INFO] 创建虚拟环境...
    python -m venv venv
)
call venv\Scripts\activate.bat
echo [OK] 已激活虚拟环境
echo.

REM ---- 3. Upgrade pip ----
echo [INFO] 升级 pip ...
python -m pip install --upgrade pip >nul

REM ---- 4. Install deps ----
echo [INFO] 安装依赖（首次会比较慢，请耐心等待）...
pip install -r requirements.txt
if errorlevel 1 (
    echo [ERROR] 依赖安装失败，请检查网络
    pause
    exit /b 1
)
echo [OK] 依赖安装完成
echo.

REM ---- 5. Ensure cloakbrowser binary ----
echo [INFO] 下载 CloakBrowser 二进制...
python -c "from cloakbrowser import ensure_binary; ensure_binary()"
if errorlevel 1 (
    echo [ERROR] CloakBrowser 下载失败
    pause
    exit /b 1
)
echo [OK] CloakBrowser 就绪
echo.

REM ---- 6. Collect credentials ----
echo ============================================================
echo   请输入你的 Zampto 凭证（输入密码时不显示字符）
echo ============================================================
set /p "ZAMPTO_USERNAME=请输入 Zampto 邮箱: "
set /p "ZAMPTO_SERVER_ID=请输入 Server ID (例如 6578): "

REM Use powershell to read password securely
powershell -Command "$pwd = Read-Host '请输入 Zampto 密码' -AsSecureString; $BSTR=[System.Runtime.InteropServices.Marshal]::SecureStringToBSTR($pwd); [System.Runtime.InteropServices.Marshal]::PtrToStringAuto($BSTR)" > "%TEMP%\zampto_pwd.txt"
set /p "ZAMPTO_PASSWORD=" < "%TEMP%\zampto_pwd.txt"
del "%TEMP%\zampto_pwd.txt"
echo.

if "!ZAMPTO_USERNAME!"=="" (
    echo [ERROR] 邮箱不能为空
    pause
    exit /b 1
)
if "!ZAMPTO_PASSWORD!"=="" (
    echo [ERROR] 密码不能为空
    pause
    exit /b 1
)
if "!ZAMPTO_SERVER_ID!"=="" (
    echo [ERROR] Server ID 不能为空
    pause
    exit /b 1
)

REM ---- 7. Set env vars and run ----
set "ZAMPTO_USERNAME=!ZAMPTO_USERNAME!"
set "ZAMPTO_PASSWORD=!ZAMPTO_PASSWORD!"
set "ZAMPTO_SERVER_ID=!ZAMPTO_SERVER_ID!"

echo ============================================================
echo   即将启动浏览器，请在浏览器中完成登录：
echo   1. 输入邮箱和密码
echo   2. 完成 Cloudflare Turnstile 验证
echo   3. 点击登录按钮
echo   4. 等待跳转后，回到此窗口按 Enter
echo ============================================================
echo.
echo [INFO] 启动脚本中...
echo.
python zampto_auto.py
echo.

REM ---- 8. Generate base64 ----
if not exist ".\screenshots\session.json" (
    echo [ERROR] 未找到 session.json，登录可能失败
    echo         请检查浏览器是否成功登录并跳转
    pause
    exit /b 1
)

echo ============================================================
echo   登录成功！正在生成 GitHub Secret 字符串...
echo ============================================================
echo.

python -c "import json,base64; s=json.load(open('./screenshots/session.json',encoding='utf-8')); print(base64.b64encode(json.dumps(s).encode()).decode())" > "%TEMP%\zampto_b64.txt"

echo 下面这串字符串就是 ZAMPTO_SESSION_SECRET 的值：
echo.
echo ----------------------------------------------------------------
type "%TEMP%\zampto_b64.txt"
echo ----------------------------------------------------------------
echo.

REM ---- 9. Save to file ----
type "%TEMP%\zampto_b64.txt" > "%WORKDIR%\SESSION_SECRET.txt"
del "%TEMP%\zampto_b64.txt"

echo [OK] 字符串已保存到: %WORKDIR%\SESSION_SECRET.txt
echo.
echo ============================================================
echo   接下来请手动操作：
echo   1. 打开 https://github.com/weikkadd/zampto/settings/secrets/actions
echo   2. 点击 New repository secret
echo   3. Name:  ZAMPTO_SESSION_SECRET
echo   4. Value: 粘贴上面那串字符串
echo   5. 同时确保 ZAMPTO_USERNAME / ZAMPTO_PASSWORD /
echo      ZAMPTO_SERVER_ID / TG_BOT_TOKEN / TG_CHAT_ID 都已添加
echo   6. 完成后在 Actions 页面手动 Run workflow 验证
echo ============================================================
echo.
pause

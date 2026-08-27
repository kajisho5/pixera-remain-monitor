@echo off
title SW PIXERA - 接続テスト
cd /d "%‾dp0"
set SCRIPT=%‾dp0sw_pixera_monitor.py

echo Companion の PIXERA 接続設定に入っている IP とポートを入れてください。
echo 例: 192.168.0.10:1400
echo.
set /p TARGET=IP:ポート = 

where py >nul 2>&1 && goto PY
where python >nul 2>&1 && goto PYTHON
echo Python が見つかりません。
pause
goto END

:PY
py "%SCRIPT%" --try %TARGET%
goto END

:PYTHON
python "%SCRIPT%" --try %TARGET%

:END
echo.
pause

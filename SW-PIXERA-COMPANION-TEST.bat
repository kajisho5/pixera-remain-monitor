@echo off
title SW PIXERA - Companion 書き込みテスト
cd /d "%‾dp0"
set SCRIPT=%‾dp0sw_pixera_monitor.py

echo Companion が動いている PC の IP を入れてください。
echo 例: 192.168.0.28
echo.
set /p TARGET=Companion の IP = 

where py >nul 2>&1 && goto PY
where python >nul 2>&1 && goto PYTHON
echo Python が見つかりません。
pause
goto END

:PY
py "%SCRIPT%" --companion-test %TARGET%
goto END

:PYTHON
python "%SCRIPT%" --companion-test %TARGET%

:END
echo.
pause

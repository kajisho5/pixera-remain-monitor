@echo off
title SW PIXERA MONITOR
cd /d "%‾dp0"
set SCRIPT=%‾dp0sw_pixera_monitor.py

where pyw >nul 2>&1 && goto PYW
where pythonw >nul 2>&1 && goto PYTHONW
where py >nul 2>&1 && goto PY
where python >nul 2>&1 && goto PYTHON
goto NOPY

:PYW
start "" pyw "%SCRIPT%" --gui
goto END

:PYTHONW
start "" pythonw "%SCRIPT%" --gui
goto END

:PY
py "%SCRIPT%" --gui
goto END

:PYTHON
python "%SCRIPT%" --gui
goto END

:NOPY
echo.
echo  Python が見つかりません。
echo  python.org からインストールしてください。
echo  インストーラの "Add python.exe to PATH" に必ずチェックを入れてください。
echo.
pause

:END

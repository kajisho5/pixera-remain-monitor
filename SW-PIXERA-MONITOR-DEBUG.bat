@echo off
title SW PIXERA MONITOR - 診断
cd /d "%‾dp0"
set SCRIPT=%‾dp0sw_pixera_monitor.py

echo ============ Python の場所 ============
where py
where python
where pyw
where pythonw
echo.
echo ============ 環境診断 ============
where py >nul 2>&1 && goto CHECK_PY
where python >nul 2>&1 && goto CHECK_PYTHON
echo Python が見つかりません。python.org から導入してください。
goto END

:CHECK_PY
py "%SCRIPT%" --check
echo.
echo ============ この窓のまま GUI 起動を試します ============
py "%SCRIPT%" --gui
goto END

:CHECK_PYTHON
python "%SCRIPT%" --check
echo.
echo ============ この窓のまま GUI 起動を試します ============
python "%SCRIPT%" --gui

:END
echo.
echo ============ 終了しました（エラーは上に出ています） ============
pause

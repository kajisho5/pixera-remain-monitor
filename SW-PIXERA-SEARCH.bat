@echo off
title SW PIXERA - ネットワーク検索
cd /d "%‾dp0"
set SCRIPT=%‾dp0sw_pixera_monitor.py

where py >nul 2>&1 && goto PY
where python >nul 2>&1 && goto PYTHON
echo Python が見つかりません。
pause
goto END

:PY
py "%SCRIPT%" --scan
goto FIN

:PYTHON
python "%SCRIPT%" --scan

:FIN
echo.
echo 見つからない場合:
echo  1. PIXERA の Settings ^> API で JSON/TCP (dl) にポートを割当て、PIXERA を再起動
echo  2. このPCと PIXERA が同じネットワーク/セグメントか確認
echo  3. IP が分かっているなら: py sw_pixera_monitor.py --scan-host 192.168.0.10
echo.

:END
pause

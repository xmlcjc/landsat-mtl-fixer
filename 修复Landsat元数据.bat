@echo off
rem ASCII-only batch file: cmd.exe mis-parses UTF-8 Chinese inside .bat on GBK consoles.
rem All Chinese messages are printed by Python (fix_mtl.py / gui_mtl.py).
chcp 65001 >nul
setlocal
set "PYTHONUTF8=1"
set "DIR=%~dp0"

set "PY="
where python >nul 2>nul
if not errorlevel 1 set "PY=python"
if not defined PY (
  where py >nul 2>nul
  if not errorlevel 1 set "PY=py -3"
)

if not defined PY goto NOPYTHON
if "%~1"=="" goto GUI

%PY% "%DIR%fix_mtl.py" %*
echo.
pause
goto END

:NOPYTHON
echo.
echo Python 3 was not found. Please install Python 3 and add it to PATH,
echo or use the packaged .exe instead.
echo.
pause
goto END

:GUI
where pythonw >nul 2>nul
if not errorlevel 1 goto GUI_W
%PY% "%DIR%gui_mtl.py"
goto END
:GUI_W
start "" pythonw "%DIR%gui_mtl.py"
goto END

:END
endlocal

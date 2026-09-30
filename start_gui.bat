@echo off
setlocal
rem ---------------------------------------------------------------
rem  lyric-tag-translator GUI launcher
rem
rem  ASCII only, CRLF. Do NOT save this file as UTF-8 with non-ASCII
rem  characters: cmd.exe reads .bat bytes in the OEM codepage, and a
rem  Chinese Windows would show mojibake (or fail to parse).
rem
rem  Every exit path ends with `pause`, so the window can never flash
rem  and vanish before you can read the error.
rem ---------------------------------------------------------------

rem  "%~dp0." (with the dot) instead of "%~dp0": the latter expands to a
rem  path ending in a backslash, which can swallow the closing quote.
cd /d "%~dp0."

set "MOD="
set "RC=0"

echo.
echo   lyric-tag-translator  /  GUI
echo   ==============================================
echo.

where python >nul 2>nul
if errorlevel 1 goto nopython

echo   Python:
python -V
echo.

python -c "import mutagen" >nul 2>nul
if not errorlevel 1 goto hasmutagen
echo   [WARN] mutagen not found, installing...
python -m pip install mutagen
echo.
:hasmutagen

rem  Prefer the new package name; fall back to the old one so this
rem  launcher keeps working with pre-rename copies of the code.
python -c "import lyric_tag_translator" >nul 2>nul
if not errorlevel 1 set "MOD=lyric_tag_translator.gui"
if defined MOD goto launch

python -c "import lyric_i18n" >nul 2>nul
if not errorlevel 1 set "MOD=lyric_i18n.gui"
if defined MOD goto launch

goto nomodule

:launch
echo   Launching: python -m %MOD%
echo.
echo   ------------------------------------------------------------
python -m %MOD%
set "RC=%ERRORLEVEL%"
echo   ------------------------------------------------------------
echo.

if not "%RC%"=="0" goto failed
echo   GUI closed normally.
echo.
pause
exit /b 0

:failed
echo   [ERROR] exit code %RC%
echo   Scroll up for the Python traceback.
echo.
pause
exit /b %RC%

:nopython
echo   [ERROR] python not found in PATH.
echo   Re-install Python 3.10+ and tick "Add Python to PATH".
echo.
pause
exit /b 1

:nomodule
echo   [ERROR] Cannot find the program.
echo   Looked for: lyric_tag_translator.gui   and   lyric_i18n.gui
echo   This .bat must sit next to the package folder.
echo.
echo   Files in this folder:
dir /b
echo.
pause
exit /b 1

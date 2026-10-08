@echo off
REM Windows-Build: erzeugt dist\EsxiBackupper.exe und dist\EsxiBackupperCli.exe
REM Voraussetzung: Python 3.10+ von python.org ist installiert.
REM Aufruf aus dem Projektordner: build\build_win.bat

cd /d "%~dp0.."

if not exist .venv (
    echo Erstelle virtuelle Umgebung ...
    python -m venv .venv || goto :error
)

echo Installiere Abhaengigkeiten ...
.venv\Scripts\pip install --upgrade pip >nul
.venv\Scripts\pip install -r requirements.txt pyinstaller || goto :error

echo Baue Exe ...
.venv\Scripts\pyinstaller --noconfirm --distpath dist --workpath build\_work build\win.spec || goto :error

echo.
echo Fertig: dist\EsxiBackupper.exe (GUI) und dist\EsxiBackupperCli.exe (Taskplaner)
exit /b 0

:error
echo BUILD FEHLGESCHLAGEN.
exit /b 1

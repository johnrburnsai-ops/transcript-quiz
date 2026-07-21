@echo off
setlocal EnableExtensions
cd /d "%~dp0"

set "ROOT=%~dp0"
set "VENV=%ROOT%.venv"
set "PYTHON=%VENV%\Scripts\python.exe"
set "DIST=%ROOT%dist\TranscriptQuiz"

echo Transcript Quiz Windows build
echo.
echo External prerequisite: OpenAI Codex CLI 0.144.5.
echo Codex CLI 0.144.5 is external and is not bundled into the executable.
echo This script does not install Node.js or Codex; install Codex separately or set CODEX_CLI_PATH when launching.
echo.

if not exist "%PYTHON%" (
    echo Creating virtual environment in "%VENV%" ...
    where py.exe >nul 2>&1
    if not errorlevel 1 (
        py -3.11 --version >nul 2>&1
        if not errorlevel 1 (
            py -3.11 -m venv "%VENV%"
        ) else (
            py -3 -m venv "%VENV%"
        )
    ) else (
        where python.exe >nul 2>&1
        if errorlevel 1 (
            echo ERROR: Python 3.11 or newer was not found.
            goto :failure
        )
        python -m venv "%VENV%"
    )
    if errorlevel 1 (
        echo ERROR: Could not create the virtual environment.
        goto :failure
    )
)

if not exist "%PYTHON%" (
    echo ERROR: The virtual environment does not contain Scripts\python.exe.
    goto :failure
)

"%PYTHON%" -c "import sys; raise SystemExit(0 if sys.version_info >= (3, 11) else 'Python 3.11 or newer is required')"
if errorlevel 1 (
    echo ERROR: The build virtual environment needs Python 3.11 or newer.
    goto :failure
)

echo Installing Python requirements ...
"%PYTHON%" -m pip install -r "%ROOT%requirements.txt"
if errorlevel 1 (
    echo ERROR: Python requirements could not be installed.
    goto :failure
)

"%PYTHON%" -m PyInstaller --version >nul 2>&1
if errorlevel 1 (
    echo PyInstaller was not found; installing it in the virtual environment ...
    "%PYTHON%" -m pip install PyInstaller
    if errorlevel 1 (
        echo ERROR: PyInstaller could not be installed.
        goto :failure
    )
)

echo Removing previous PyInstaller output ...
if exist "%ROOT%build" rmdir /s /q "%ROOT%build"
if exist "%DIST%" rmdir /s /q "%DIST%"
if exist "%DIST%" (
    echo ERROR: Could not remove the previous output folder.
    goto :failure
)

echo Building the onedir windowed application ...
"%PYTHON%" -m PyInstaller --noconfirm --clean --distpath "%ROOT%dist" --workpath "%ROOT%build" "%ROOT%transcript_quiz.spec"
if errorlevel 1 (
    echo ERROR: PyInstaller failed.
    goto :failure
)

if not exist "%DIST%\TranscriptQuiz.exe" (
    echo ERROR: The expected executable was not produced.
    goto :failure
)

copy /y "%ROOT%LaunchTranscriptQuiz.vbs" "%DIST%\" >nul
if errorlevel 1 (
    echo ERROR: Could not copy LaunchTranscriptQuiz.vbs to the distribution folder.
    goto :failure
)
copy /y "%ROOT%LaunchTranscriptQuiz.cmd" "%DIST%\" >nul
if errorlevel 1 (
    echo ERROR: Could not copy LaunchTranscriptQuiz.cmd to the distribution folder.
    goto :failure
)

echo.
echo Build complete.
echo Output: "%DIST%"
echo Distribute the entire folder, including its _internal files and launchers.
echo Codex CLI 0.144.5 is still required on the destination machine or via CODEX_CLI_PATH.
exit /b 0

:failure
echo.
echo Build failed.
exit /b 1

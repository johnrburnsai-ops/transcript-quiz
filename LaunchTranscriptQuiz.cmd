@echo off
setlocal EnableExtensions DisableDelayedExpansion
set "ROOT=%~dp0"
cd /d "%ROOT%"
if errorlevel 1 (
    echo ERROR: Could not change to "%ROOT%".
    pause
    exit /b 1
)

rem Add common Codex locations to PATH, then prefer an explicit CODEX_CLI_PATH.
call :add_path "%ROOT%"
call :add_path "%APPDATA%\npm"
call :add_path "%LOCALAPPDATA%\npm"
call :add_path "%LOCALAPPDATA%\Programs\nodejs"
call :add_path "%ProgramFiles%\nodejs"
call :add_path "%ProgramFiles(x86)%\nodejs"
call :add_path "%NVM_SYMLINK%"
call :add_path "%USERPROFILE%\.npm-global\bin"
call :add_path "%USERPROFILE%\.local\bin"
call :add_path "%USERPROFILE%\.codex\bin"
call :add_path "%LOCALAPPDATA%\Codex"
call :add_path "%LOCALAPPDATA%\Programs\Codex"
call :add_path "%LOCALAPPDATA%\OpenAI\Codex"
call :add_path "%LOCALAPPDATA%\Programs\OpenAI\Codex"
call :add_path "%ProgramFiles%\Codex"
call :add_path "%ProgramFiles%\OpenAI\Codex"
call :add_path "%ProgramFiles(x86)%\Codex"
call :add_path "%ProgramFiles(x86)%\OpenAI\Codex"

set "EXPLICIT_CODEX=%CODEX_CLI_PATH%"
set "EXPLICIT_CODEX=%EXPLICIT_CODEX:"=%"
if defined EXPLICIT_CODEX (
    set "CODEX_CLI_PATH=%EXPLICIT_CODEX%"
    for %%P in ("%EXPLICIT_CODEX%") do call :add_path "%%~dpP"
) else (
    call :find_codex "%APPDATA%\npm"
    call :find_codex "%LOCALAPPDATA%\npm"
    call :find_codex "%LOCALAPPDATA%\Programs\nodejs"
    call :find_codex "%ProgramFiles%\nodejs"
    call :find_codex "%ProgramFiles(x86)%\nodejs"
    call :find_codex "%NVM_SYMLINK%"
    call :find_codex "%USERPROFILE%\.npm-global\bin"
    call :find_codex "%USERPROFILE%\.local\bin"
    call :find_codex "%USERPROFILE%\.codex\bin"
    call :find_codex "%LOCALAPPDATA%\Codex"
    call :find_codex "%LOCALAPPDATA%\Programs\Codex"
    call :find_codex "%LOCALAPPDATA%\OpenAI\Codex"
    call :find_codex "%LOCALAPPDATA%\Programs\OpenAI\Codex"
    call :find_codex "%ProgramFiles%\Codex"
    call :find_codex "%ProgramFiles%\OpenAI\Codex"
    call :find_codex "%ProgramFiles(x86)%\Codex"
    call :find_codex "%ProgramFiles(x86)%\OpenAI\Codex"
    if not defined CODEX_CLI_PATH for /f "delims=" %%P in ('where.exe codex 2^>nul') do if not defined CODEX_CLI_PATH set "CODEX_CLI_PATH=%%~fP"
)

if not defined CODEX_CLI_PATH echo WARNING: Codex CLI was not found. Install Codex 0.144.5 or set CODEX_CLI_PATH before launching.

if exist "%ROOT%TranscriptQuiz.exe" goto :run_frozen
if exist "%ROOT%.venv\Scripts\python.exe" if exist "%ROOT%main.py" goto :run_source
goto :missing

:run_frozen
"%ROOT%TranscriptQuiz.exe"
set "EXIT_CODE=%ERRORLEVEL%"
if not "%EXIT_CODE%"=="0" (
    echo.
    echo TranscriptQuiz.exe exited with code %EXIT_CODE%.
    pause
)
exit /b %EXIT_CODE%

:run_source
rem The CMD launcher intentionally uses python.exe so startup tracebacks stay visible.
"%ROOT%.venv\Scripts\python.exe" "%ROOT%main.py"
set "EXIT_CODE=%ERRORLEVEL%"
if not "%EXIT_CODE%"=="0" (
    echo.
    echo Transcript Quiz exited with code %EXIT_CODE%.
    pause
)
exit /b %EXIT_CODE%

:missing
echo.
echo Transcript Quiz could not be started.
echo.
echo Expected either:
echo   "%ROOT%TranscriptQuiz.exe"
echo or the source-tree files:
echo   "%ROOT%.venv\Scripts\python.exe"
echo   "%ROOT%.venv\Scripts\pythonw.exe"  (used by the silent VBS launcher)
echo   "%ROOT%main.py"
echo.
echo Put this launcher beside the complete PyInstaller output folder, or run build_exe.cmd first.
pause
exit /b 1

:add_path
if "%~1"=="" exit /b 0
if exist "%~1\" set "PATH=%PATH%;%~1"
exit /b 0

:find_codex
if defined CODEX_CLI_PATH exit /b 0
if exist "%~1\codex.exe" (
    set "CODEX_CLI_PATH=%~1\codex.exe"
    exit /b 0
)
if exist "%~1\codex.cmd" (
    set "CODEX_CLI_PATH=%~1\codex.cmd"
    exit /b 0
)
if exist "%~1\codex.bat" (
    set "CODEX_CLI_PATH=%~1\codex.bat"
    exit /b 0
)
if exist "%~1\codex" (
    set "CODEX_CLI_PATH=%~1\codex"
    exit /b 0
)
exit /b 0

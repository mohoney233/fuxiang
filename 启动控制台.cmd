@echo off
rem Fuxiang bridge console launcher: build first if exe is missing, then start it.
rem NOTE: keep this file pure ASCII with CRLF line endings. cmd.exe parses batch
rem files in the ANSI codepage (GBK on Chinese Windows); UTF-8 Chinese text here
rem gets mis-decoded and breaks the script before "start" runs.
setlocal
cd /d "%~dp0"

rem Git Bash and similar environments export TEMP as a POSIX path, which makes
rem older XamlCompiler versions fail silently. Force Windows-style temp paths.
set "TEMP=%LOCALAPPDATA%\Temp"
set "TMP=%LOCALAPPDATA%\Temp"

rem Launcher sits at the repo root; the app itself lives under console\.
set "EXE=console\bin\x64\Debug\net8.0-windows10.0.19041.0\fuxiang-console.exe"

if not exist "%EXE%" (
    echo [console] First run: building...
    dotnet build console\fuxiang-console.csproj -c Debug -p:Platform=x64
    if errorlevel 1 (
        echo [console] Build failed, see output above.
        pause
        exit /b 1
    )
)

start "" "%EXE%"
endlocal

@echo off
rem Starts the Omnigraph Inbox app and opens it in your browser.
rem Data folder: OMNIGRAPH_KNOWLEDGE if set, else the "knowledge" folder next to this clone.
setlocal
if "%OMNIGRAPH_KNOWLEDGE%"=="" set "OMNIGRAPH_KNOWLEDGE=%~dp0..\..\..\knowledge"
python "%~dp0inbox_app.py" --data-dir "%OMNIGRAPH_KNOWLEDGE%" serve
if errorlevel 1 pause

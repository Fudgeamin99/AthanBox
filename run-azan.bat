@echo off
rem Starts Azan SIP with a console window (for troubleshooting).
rem Normally just double-click "Azan SIP" instead.
cd /d "%~dp0"
python -m azan_sip -c config.toml panel
pause

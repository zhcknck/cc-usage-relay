@echo off
rem Claude Code Stop hook: launch agent.py detached via start /b (returns immediately,
rem never blocks Claude Code). Keep this file pure ASCII - cmd parses it in the OEM codepage.
where pythonw >nul 2>nul
if %errorlevel%==0 (
  start "" /b pythonw "%~dp0..\agent\agent.py" hook
) else (
  start "" /b python "%~dp0..\agent\agent.py" hook
)
exit /b 0

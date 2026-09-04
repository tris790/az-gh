@echo off
setlocal
python "%~dp0gh.py" %*
exit /b %ERRORLEVEL%

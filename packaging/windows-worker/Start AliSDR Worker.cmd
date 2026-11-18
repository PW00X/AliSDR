@echo off
setlocal
pushd "%~dp0" || exit /b 1
set "PYTHONHOME="
set "PYTHONPATH="
set "PYTHONNOUSERSITE=1"
"%~dp0runtime\python.exe" "%~dp0launch.py" %*
set "result=%errorlevel%"
popd
if not "%result%"=="0" pause
exit /b %result%

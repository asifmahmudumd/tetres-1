@echo off
cd %~dp0
cd
python36\python.exe Server\src\server.py
ECHO Press any key to exit
PAUSE >NUL
EXIT /B
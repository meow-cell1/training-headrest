@echo off
cd /d %~dp0
tailwindcss.exe -i static\src.css -o static\app.css --minify
pause

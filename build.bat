@echo off
python -m pip install --upgrade pyinstaller
python -m PyInstaller --onefile --windowed --name PCPowerCalc app.py
echo.
echo Done: dist\PCPowerCalc.exe
pause

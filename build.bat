@echo off
rem Builds dist\SwiftMyPC.exe (single file, no Python needed to run it).
cd /d "%~dp0"
python -m pip install -q -r requirements.txt pyinstaller
python -m PyInstaller --noconfirm --clean --onefile --windowed --name SwiftMyPC ^
  --icon assets\icon.ico --add-data "assets\icon.ico;assets" --add-data "assets\icon.png;assets" ^
  --hidden-import pystray._win32 --version-file version_info.txt ^
  --workpath "%TEMP%\swiftmypc-build" launcher.py

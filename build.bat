@echo off
rem Builds dist\SwiftMyPC.exe (single file, no Python needed to run it)
rem and dist\SwiftMyPC.exe.sha256, which the auto-updater requires in every release.
cd /d "%~dp0"
python -m pip install -q -r requirements.txt pyinstaller
python -m PyInstaller --noconfirm --clean --onefile --windowed --name SwiftMyPC ^
  --icon assets\icon.ico --add-data "assets\icon.ico;assets" --add-data "assets\icon.png;assets" ^
  --add-data "swiftmypc\ui;swiftmypc\ui" ^
  --hidden-import pystray._win32 --version-file version_info.txt ^
  --workpath "%TEMP%\swiftmypc-build" launcher.py || exit /b 1
python -c "import hashlib; h = hashlib.sha256(open(r'dist\SwiftMyPC.exe', 'rb').read()).hexdigest(); open(r'dist\SwiftMyPC.exe.sha256', 'w').write(h + '  SwiftMyPC.exe')"

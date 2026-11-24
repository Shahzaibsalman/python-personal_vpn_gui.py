@echo off
REM Builds PersonalVPN.exe on Windows. Put this file next to
REM personal_vpn_gui.py, logo.png and logo.ico, then double-click it.

py -m venv .venv
call .venv\Scripts\activate.bat
python -m pip install --upgrade pip
pip install "flet>=1.0,<2" pyinstaller

flet pack personal_vpn_gui.py --name PersonalVPN --icon logo.ico --add-data "logo.png;." --add-data "logo.ico;." --uac-admin -y

echo.
echo Done. Your app is at dist\PersonalVPN.exe
pause

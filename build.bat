@echo off
rem Builds dist\RenderKiller.exe. First run creates the .venv with PyInstaller.
cd /d "%~dp0"
if not exist .venv\Scripts\pyinstaller.exe (
	py -3.14 -m venv .venv || exit /b 1
	.venv\Scripts\python -m pip install -q pyinstaller || exit /b 1
)
.venv\Scripts\pyinstaller --noconfirm --clean --onefile --windowed ^
	--name RenderKiller --icon icon.ico --add-data "icon.ico;." ^
	render_killer.pyw || exit /b 1
echo Built dist\RenderKiller.exe

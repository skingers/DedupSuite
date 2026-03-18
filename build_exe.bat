@echo off
echo ========================================================
echo      DEDUP SUITE - EXE BUILDER
echo ========================================================
echo.

set APP_VERSION=v1.1.1

echo [1/3] Installing/Verifying PyInstaller...
python -m pip install pyinstaller --upgrade

echo [2/3] Building Executable (This may take a minute)...
:: --noconsole: Hides the black terminal window
:: --onefile: Bundles everything into a single .exe
:: --collect-all customtkinter: Ensures the UI theme files are included
:: --clean: Clears PyInstaller cache to prevent errors
:: --add-data "app.ico;.": Bundles the icon file so the app can find it
:: --icon="app.ico": Sets the icon for the .exe file itself
python -m PyInstaller --noconsole --onefile --clean --collect-all customtkinter --add-data "app.ico;." --icon="app.ico" --name "DedupSuite_%APP_VERSION%" dedup_suite.py

echo.
echo ========================================================
if exist "dist\DedupSuite_%APP_VERSION%.exe" (
    echo    SUCCESS! Your new application is in the 'dist' folder.
) else (
    echo    ERROR: Build failed. Check the output above.
)
echo ========================================================
pause
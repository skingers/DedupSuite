@echo off
title DedupSuite Builder
echo ==========================================
echo      DedupSuite EXE Builder
echo ==========================================

REM 1. Install Build Dependencies
echo [INFO] Checking dependencies...
pip install pyinstaller customtkinter pillow opencv-python-headless imagehash reportlab

REM 2. Icon Check
if not exist "app.ico" (
    echo [WARNING] 'app.ico' not found.
    if exist "convert_icon.py" (
        echo [INFO] Attempting to generate icon from 'logo.png'...
        python convert_icon.py
    )
)

REM 3. Build Command
echo.
echo [INFO] Starting PyInstaller build...
echo This may take a few minutes.
echo.

REM Explanation of flags:
REM --noconsole: Hides the black command window when running the app.
REM --onefile: Bundles everything into a single .exe file.
REM --add-data "app.ico;.": Bundles the icon inside the exe for the window title bar.
REM --collect-all customtkinter: CRITICAL. Copies CTK themes and json files.
REM --icon "app.ico": Sets the file icon for Windows Explorer.

python -m PyInstaller --noconsole --onefile --name="DedupSuite" --icon="app.ico" --version-file="version_info.txt" --add-data="app.ico;." --collect-all customtkinter dedup_suite.py

echo.

REM 4. Code Signing (Optional)
if exist "certificate.pfx" (
    echo [INFO] Found certificate.pfx. Attempting to sign...
    REM Note: signtool.exe must be in your PATH (install Windows SDK)
    REM Replace 'YourCertPassword' below with the actual password for your PFX file
    signtool sign /f "certificate.pfx" /p "YourCertPassword" /tr http://timestamp.digicert.com /td sha256 /fd sha256 "dist\DedupSuite.exe"
)

if exist "dist\DedupSuite.exe" (
    echo ==========================================
    echo [SUCCESS] Build Complete!
    echo Your app is located in the 'dist' folder.
    echo ==========================================
    explorer dist
) else (
    echo [FAILURE] Build failed. Please check the error messages above.
)
pause

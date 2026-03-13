@echo off
echo ========================================================
echo      DEDUP SUITE - AUTO GITLAB UPLOADER
echo ========================================================
echo.

:: 1. Fix the destination address (just in case it was wrong before)
echo [1/4] Configuring GitLab address...
git remote remove origin >nul 2>&1
git remote add origin https://gitlab.com/skingers/DedupSuite.git

:: 2. Stage all changes (new files, modified files, deletions)
echo [2/4] Gathering all file changes...
git add .

:: 3. Save the changes locally
echo [3/4] Saving snapshot...
git commit -m "Update via Auto-Uploader"

:: 4. Upload to GitLab
echo [4/4] Uploading to GitLab...
git push -u origin main

echo.
echo ========================================================
if %ERRORLEVEL% EQU 0 (
    echo    SUCCESS! Your code is now on GitLab.
) else (
    echo    ERROR: Something went wrong. Check the message above.
)
echo ========================================================
pause

@echo off
echo ========================================================
echo      sovraan - LOCAL GIT COMMIT
echo ========================================================
echo.

:: 1. Stage all changes (new files, modified files, deletions)
echo [1/2] Gathering all file changes...
git add .

:: 2. Save the changes locally
echo [2/2] Saving snapshot...
git commit -m "Local Update"

echo.
echo ========================================================
echo    SUCCESS! Your code is committed locally.
echo ========================================================
pause

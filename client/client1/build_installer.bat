@echo off
setlocal enabledelayedexpansion

echo ============================================================
echo   FoxNest Installer Builder
echo ============================================================
echo.

:: Check Python
py --version >nul 2>&1
if errorlevel 1 (
    echo [ERROR] Python not found!
    echo Please install Python from https://python.org
    pause
    exit /b 1
)

echo [OK] Found Python
py --version
echo.

:: Check if fox.py exists
if not exist "fox.py" (
    echo [ERROR] fox.py not found!
    echo Please run this script from the client directory.
    pause
    exit /b 1
)

echo [OK] Found fox.py
echo.

:: Install dependencies
echo Installing dependencies...
py -m pip install pyinstaller requests >nul 2>&1
echo [OK] Dependencies installed
echo.

:: Step 1: Build fox.exe first
echo ============================================================
echo Step 1: Building fox.exe (the main client)
echo ============================================================
echo.

if exist dist\fox.exe (
    echo [INFO] fox.exe already exists in dist\
    set /p REBUILD="Rebuild fox.exe? (y/n): "
    if /i "!REBUILD!"=="y" (
        echo Rebuilding fox.exe...
        if exist build rmdir /s /q build
        if exist dist rmdir /s /q dist
        py -m PyInstaller --onefile --name=fox --console fox.py
    )
) else (
    echo Building fox.exe...
    if exist build rmdir /s /q build
    if exist dist rmdir /s /q dist
    py -m PyInstaller --onefile --name=fox --console fox.py
)

if not exist dist\fox.exe (
    echo [ERROR] Failed to build fox.exe!
    pause
    exit /b 1
)

echo [OK] fox.exe built successfully
for %%A in (dist\fox.exe) do echo     Size: %%~zA bytes
echo.

:: Step 2: Build the installer
echo ============================================================
echo Step 2: Building FoxNest-Setup.exe (the installer)
echo ============================================================
echo.

:: Clean up old installer build
if exist build\fox_installer rmdir /s /q build\fox_installer
if exist dist\FoxNest-Setup.exe del /f /q dist\FoxNest-Setup.exe

echo Building installer...
py -m PyInstaller --clean fox_installer.spec

if not exist dist\FoxNest-Setup.exe (
    echo [ERROR] Failed to build installer!
    pause
    exit /b 1
)

echo.
echo [OK] Installer built successfully!
for %%A in (dist\FoxNest-Setup.exe) do echo     Size: %%~zA bytes
echo.

:: Summary
echo ============================================================
echo   BUILD COMPLETE!
echo ============================================================
echo.
echo Created files:
echo   dist\fox.exe          - The Fox client (command-line tool)
echo   dist\FoxNest-Setup.exe - The installer (distributable)
echo.
echo To install FoxNest on any Windows PC:
echo   1. Copy FoxNest-Setup.exe to the target computer
echo   2. Double-click to run the installer
echo   3. Follow the installation wizard
echo.
echo After installation, open a NEW terminal and type: fox --help
echo.
pause


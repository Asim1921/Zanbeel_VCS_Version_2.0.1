@echo off
setlocal enabledelayedexpansion

echo.
echo ============================================================
echo   FoxNest Complete Build Script
echo   Creates fox.exe and FoxNest-Setup.exe installer
echo ============================================================
echo.

:: Check Python
py --version >nul 2>&1
if errorlevel 1 (
    python --version >nul 2>&1
    if errorlevel 1 (
        echo [ERROR] Python not found!
        echo Please install Python from https://python.org
        echo Make sure to check "Add Python to PATH" during installation.
        pause
        exit /b 1
    )
    set PYTHON=python
) else (
    set PYTHON=py
)

echo [OK] Found Python
%PYTHON% --version
echo.

:: Check if fox.py exists
if not exist "fox.py" (
    echo [ERROR] fox.py not found!
    echo Please run this script from the FoxNest-main\client directory.
    echo.
    echo Current directory: %CD%
    pause
    exit /b 1
)

echo [OK] Found fox.py
echo.

:: Install dependencies
echo Installing dependencies...
%PYTHON% -m pip install pyinstaller requests --quiet
if errorlevel 1 (
    echo [ERROR] Failed to install dependencies!
    pause
    exit /b 1
)
echo [OK] Dependencies installed
echo.

:: Clean up old builds
echo Cleaning up old builds...
if exist build rmdir /s /q build 2>nul
if exist dist rmdir /s /q dist 2>nul
if exist __pycache__ rmdir /s /q __pycache__ 2>nul
echo [OK] Cleaned up
echo.

:: ============================================================
:: Step 1: Build fox.exe
:: ============================================================
echo ============================================================
echo Step 1: Building fox.exe (command-line client)
echo ============================================================
echo.

%PYTHON% -m PyInstaller ^
    --onefile ^
    --name=fox ^
    --console ^
    --clean ^
    --noconfirm ^
    fox.py

if errorlevel 1 (
    echo.
    echo [ERROR] Failed to build fox.exe!
    pause
    exit /b 1
)

if not exist "dist\fox.exe" (
    echo.
    echo [ERROR] fox.exe was not created!
    pause
    exit /b 1
)

for %%A in (dist\fox.exe) do set FOX_SIZE=%%~zA
set /a FOX_SIZE_MB=%FOX_SIZE% / 1048576
echo.
echo [OK] fox.exe built successfully (%FOX_SIZE_MB% MB)
echo.

:: ============================================================
:: Step 2: Build the installer
:: ============================================================
echo ============================================================
echo Step 2: Building FoxNest-Setup.exe (installer)
echo ============================================================
echo.

:: Clean up installer build artifacts
if exist build\fox_simple_installer rmdir /s /q build\fox_simple_installer 2>nul

%PYTHON% -m PyInstaller ^
    --onefile ^
    --name=FoxNest-Setup ^
    --console ^
    --clean ^
    --noconfirm ^
    --add-data "dist\fox.exe;." ^
    fox_simple_installer.py

if errorlevel 1 (
    echo.
    echo [ERROR] Failed to build installer!
    pause
    exit /b 1
)

if not exist "dist\FoxNest-Setup.exe" (
    echo.
    echo [ERROR] Installer was not created!
    pause
    exit /b 1
)

for %%A in (dist\FoxNest-Setup.exe) do set SETUP_SIZE=%%~zA
set /a SETUP_SIZE_MB=%SETUP_SIZE% / 1048576
echo.
echo [OK] Installer built successfully (%SETUP_SIZE_MB% MB)
echo.

:: ============================================================
:: Summary
:: ============================================================
echo.
echo ============================================================
echo   BUILD COMPLETE!
echo ============================================================
echo.
echo Created files in dist\ folder:
echo.
echo   fox.exe            - The Fox client (for developers)
echo                        Run: fox --help
echo.
echo   FoxNest-Setup.exe  - The installer (for distribution)
echo                        Double-click to install on any Windows PC
echo.
echo ============================================================
echo.
echo To distribute FoxNest:
echo   1. Copy dist\FoxNest-Setup.exe to the target computer
echo   2. Double-click FoxNest-Setup.exe
echo   3. Follow the installation prompts
echo   4. Open a NEW terminal and type: fox --help
echo.
echo ============================================================
echo.

:: Ask if user wants to test
set /p TEST="Test the installer now? (y/n): "
if /i "%TEST%"=="y" (
    echo.
    echo Starting installer...
    start "" "dist\FoxNest-Setup.exe"
)

pause


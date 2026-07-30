#!/usr/bin/env python3
"""
Fox Client Simple Windows Installer
====================================
A simple, reliable console-based installer for the Fox client.
Works on all Windows versions without requiring any GUI libraries.

Build: pyinstaller --onefile --name=FoxNest-Setup-Console fox_simple_installer.py
"""

import os
import sys
import shutil
import ctypes
import subprocess
import tempfile
from pathlib import Path

# ============================================================================
# CONFIGURATION
# ============================================================================
APP_NAME = "FoxNest"
APP_VERSION = "1.0.1"
DEFAULT_INSTALL_DIR = os.path.join(os.environ.get('LOCALAPPDATA', os.path.expanduser('~')), 'FoxNest')

# ============================================================================
# UTILITY FUNCTIONS
# ============================================================================

def clear_screen():
    os.system('cls' if os.name == 'nt' else 'clear')

def pause():
    input("\nPress Enter to continue...")

def get_fox_exe_path():
    """Find fox.exe in various locations"""
    search_paths = [
        # If frozen, check the temp extraction directory
        Path(getattr(sys, '_MEIPASS', '')) / "fox.exe",
        # Same directory as this script/exe
        Path(sys.executable).parent / "fox.exe",
        Path(__file__).parent / "fox.exe",
        # dist folder
        Path(__file__).parent / "dist" / "fox.exe",
        Path(__file__).parent.parent / "dist" / "fox.exe",
        # Current working directory
        Path.cwd() / "fox.exe",
        Path.cwd() / "dist" / "fox.exe",
    ]
    
    for path in search_paths:
        if path.exists():
            return str(path)
    
    return None

def broadcast_environment_change():
    """Notify Windows that environment variables have changed"""
    try:
        HWND_BROADCAST = 0xFFFF
        WM_SETTINGCHANGE = 0x1A
        SMTO_ABORTIFHUNG = 0x0002
        result = ctypes.c_long()
        ctypes.windll.user32.SendMessageTimeoutW(
            HWND_BROADCAST,
            WM_SETTINGCHANGE,
            0,
            "Environment",
            SMTO_ABORTIFHUNG,
            5000,
            ctypes.byref(result)
        )
    except:
        pass

def add_to_user_path(directory):
    """Add directory to user's PATH using PowerShell"""
    try:
        # PowerShell command to add to PATH
        ps_command = f'''
$currentPath = [Environment]::GetEnvironmentVariable('Path', 'User')
$paths = $currentPath -split ';' | Where-Object {{ $_ -ne '' }}
if ($paths -notcontains '{directory}') {{
    $newPath = ($paths + '{directory}') -join ';'
    [Environment]::SetEnvironmentVariable('Path', $newPath, 'User')
    Write-Output 'ADDED'
}} else {{
    Write-Output 'EXISTS'
}}
'''
        result = subprocess.run(
            ['powershell', '-NoProfile', '-Command', ps_command],
            capture_output=True,
            text=True,
            timeout=30
        )
        
        if 'ADDED' in result.stdout:
            broadcast_environment_change()
            return True, "Added to PATH"
        elif 'EXISTS' in result.stdout:
            return True, "Already in PATH"
        else:
            return False, result.stderr or "Unknown error"
            
    except subprocess.TimeoutExpired:
        return False, "Operation timed out"
    except Exception as e:
        return False, str(e)

def remove_from_user_path(directory):
    """Remove directory from user's PATH"""
    try:
        ps_command = f'''
$currentPath = [Environment]::GetEnvironmentVariable('Path', 'User')
$paths = $currentPath -split ';' | Where-Object {{ $_ -ne '' -and $_ -ne '{directory}' }}
$newPath = $paths -join ';'
[Environment]::SetEnvironmentVariable('Path', $newPath, 'User')
Write-Output 'REMOVED'
'''
        subprocess.run(
            ['powershell', '-NoProfile', '-Command', ps_command],
            capture_output=True,
            text=True,
            timeout=30
        )
        broadcast_environment_change()
        return True
    except:
        return False

def create_uninstall_registry(install_dir):
    """Create uninstall registry entry"""
    try:
        import winreg
        key = winreg.CreateKey(
            winreg.HKEY_CURRENT_USER,
            rf"Software\Microsoft\Windows\CurrentVersion\Uninstall\{APP_NAME}"
        )
        
        uninstaller = os.path.join(install_dir, "uninstall.bat")
        
        winreg.SetValueEx(key, "DisplayName", 0, winreg.REG_SZ, f"{APP_NAME} Version Control System")
        winreg.SetValueEx(key, "DisplayVersion", 0, winreg.REG_SZ, APP_VERSION)
        winreg.SetValueEx(key, "Publisher", 0, winreg.REG_SZ, "FoxNest Team")
        winreg.SetValueEx(key, "UninstallString", 0, winreg.REG_SZ, f'cmd /c "{uninstaller}"')
        winreg.SetValueEx(key, "InstallLocation", 0, winreg.REG_SZ, install_dir)
        winreg.SetValueEx(key, "DisplayIcon", 0, winreg.REG_SZ, os.path.join(install_dir, "fox.exe"))
        winreg.SetValueEx(key, "NoModify", 0, winreg.REG_DWORD, 1)
        winreg.SetValueEx(key, "NoRepair", 0, winreg.REG_DWORD, 1)
        
        winreg.CloseKey(key)
        return True
    except Exception as e:
        print(f"  Warning: Could not create registry entry: {e}")
        return False

def remove_uninstall_registry():
    """Remove uninstall registry entry"""
    try:
        import winreg
        winreg.DeleteKey(
            winreg.HKEY_CURRENT_USER,
            rf"Software\Microsoft\Windows\CurrentVersion\Uninstall\{APP_NAME}"
        )
        return True
    except:
        return False

def create_uninstall_script(install_dir):
    """Create uninstall batch script"""
    uninstall_bat = os.path.join(install_dir, "uninstall.bat")
    
    script = f'''@echo off
echo.
echo ============================================================
echo   FoxNest Uninstaller
echo ============================================================
echo.
echo This will uninstall FoxNest from your computer.
echo Installation directory: {install_dir}
echo.
set /p CONFIRM="Are you sure you want to uninstall? (y/n): "
if /i not "%CONFIRM%"=="y" (
    echo Uninstall cancelled.
    pause
    exit /b 0
)
echo.
echo Removing from PATH...
powershell -NoProfile -Command "$p = [Environment]::GetEnvironmentVariable('Path', 'User'); $p = ($p -split ';' | Where-Object {{ $_ -ne '{install_dir}' }}) -join ';'; [Environment]::SetEnvironmentVariable('Path', $p, 'User')"
echo Removing registry entry...
reg delete "HKCU\\Software\\Microsoft\\Windows\\CurrentVersion\\Uninstall\\{APP_NAME}" /f >nul 2>&1
echo Removing files...
cd /d "%TEMP%"
rmdir /s /q "{install_dir}" 2>nul
echo.
echo ============================================================
echo   FoxNest has been uninstalled successfully!
echo ============================================================
echo.
pause
'''
    
    with open(uninstall_bat, 'w') as f:
        f.write(script)
    
    return uninstall_bat

# ============================================================================
# INSTALLER
# ============================================================================

def run_installer():
    clear_screen()
    
    print("=" * 60)
    print(f"  🦊 {APP_NAME} Installer v{APP_VERSION}")
    print("=" * 60)
    print()
    print("This will install the Fox version control client on your")
    print("computer, allowing you to use 'fox' commands from any terminal.")
    print()
    
    # Find fox.exe
    fox_exe = get_fox_exe_path()
    
    if not fox_exe:
        print("❌ ERROR: fox.exe not found!")
        print()
        print("The installer could not find fox.exe.")
        print("Please make sure fox.exe is in the same folder as this installer.")
        print()
        print("If you're building from source:")
        print("  1. Run: py -m PyInstaller --onefile --name=fox fox.py")
        print("  2. Copy dist\\fox.exe to this folder")
        print("  3. Run the installer again")
        pause()
        return False
    
    fox_size = os.path.getsize(fox_exe) / (1024 * 1024)
    print(f"✓ Found fox.exe ({fox_size:.2f} MB)")
    print()
    
    # Get installation directory
    print(f"Default installation directory:")
    print(f"  {DEFAULT_INSTALL_DIR}")
    print()
    custom = input("Use default? (Y/n): ").strip().lower()
    
    if custom == 'n':
        install_dir = input("Enter installation directory: ").strip()
        if not install_dir:
            install_dir = DEFAULT_INSTALL_DIR
    else:
        install_dir = DEFAULT_INSTALL_DIR
    
    print()
    print("Installation directory:", install_dir)
    print()
    
    # Confirm
    confirm = input("Proceed with installation? (Y/n): ").strip().lower()
    if confirm == 'n':
        print("Installation cancelled.")
        pause()
        return False
    
    print()
    print("-" * 60)
    print("Installing...")
    print("-" * 60)
    print()
    
    # Step 1: Create directory
    print("[1/5] Creating installation directory...")
    try:
        os.makedirs(install_dir, exist_ok=True)
        print(f"  ✓ Created: {install_dir}")
    except Exception as e:
        print(f"  ❌ Failed: {e}")
        pause()
        return False
    
    # Step 2: Copy fox.exe
    print("[2/5] Copying fox.exe...")
    dest_fox = os.path.join(install_dir, "fox.exe")
    try:
        shutil.copy2(fox_exe, dest_fox)
        print(f"  ✓ Copied to: {dest_fox}")
    except Exception as e:
        print(f"  ❌ Failed: {e}")
        pause()
        return False
    
    # Step 3: Add to PATH
    print("[3/5] Adding to PATH...")
    success, message = add_to_user_path(install_dir)
    if success:
        print(f"  ✓ {message}")
    else:
        print(f"  ⚠ Warning: {message}")
        print(f"  You may need to add manually: {install_dir}")
    
    # Step 4: Create uninstaller
    print("[4/5] Creating uninstaller...")
    try:
        uninstall_bat = create_uninstall_script(install_dir)
        print(f"  ✓ Created: {uninstall_bat}")
    except Exception as e:
        print(f"  ⚠ Warning: {e}")
    
    # Step 5: Register with Windows
    print("[5/5] Registering with Windows...")
    if create_uninstall_registry(install_dir):
        print("  ✓ Registered in Programs and Features")
    else:
        print("  ⚠ Could not register (non-critical)")
    
    print()
    print("=" * 60)
    print("  ✓ INSTALLATION COMPLETE!")
    print("=" * 60)
    print()
    print("FoxNest has been installed successfully!")
    print()
    print("To get started:")
    print("  1. Open a NEW Command Prompt or PowerShell window")
    print("  2. Type: fox --help")
    print("  3. Initialize a repository: fox init")
    print()
    print("⚠️  IMPORTANT: You MUST open a NEW terminal window")
    print("    for the PATH changes to take effect!")
    print()
    print(f"Installation location: {install_dir}")
    print()
    
    pause()
    return True

# ============================================================================
# UNINSTALLER
# ============================================================================

def run_uninstaller():
    clear_screen()
    
    print("=" * 60)
    print(f"  🦊 {APP_NAME} Uninstaller")
    print("=" * 60)
    print()
    
    # Find installation directory
    install_dir = DEFAULT_INSTALL_DIR
    
    # Try to get from registry
    try:
        import winreg
        key = winreg.OpenKey(
            winreg.HKEY_CURRENT_USER,
            rf"Software\Microsoft\Windows\CurrentVersion\Uninstall\{APP_NAME}",
            0, winreg.KEY_READ
        )
        install_dir, _ = winreg.QueryValueEx(key, "InstallLocation")
        winreg.CloseKey(key)
    except:
        pass
    
    if not os.path.exists(install_dir):
        print(f"FoxNest installation not found at: {install_dir}")
        print()
        custom = input("Enter installation directory (or press Enter to cancel): ").strip()
        if not custom:
            print("Uninstall cancelled.")
            pause()
            return False
        install_dir = custom
    
    print(f"Installation directory: {install_dir}")
    print()
    
    confirm = input("Are you sure you want to uninstall FoxNest? (y/N): ").strip().lower()
    if confirm != 'y':
        print("Uninstall cancelled.")
        pause()
        return False
    
    print()
    print("Uninstalling...")
    print()
    
    # Remove from PATH
    print("[1/3] Removing from PATH...")
    if remove_from_user_path(install_dir):
        print("  ✓ Removed from PATH")
    else:
        print("  ⚠ Could not remove from PATH")
    
    # Remove registry
    print("[2/3] Removing registry entry...")
    if remove_uninstall_registry():
        print("  ✓ Removed registry entry")
    else:
        print("  ⚠ Could not remove registry entry")
    
    # Remove files
    print("[3/3] Removing files...")
    try:
        # Don't remove if we're running from install dir
        current_exe = os.path.abspath(sys.executable)
        if not current_exe.startswith(os.path.abspath(install_dir)):
            shutil.rmtree(install_dir)
            print(f"  ✓ Removed: {install_dir}")
        else:
            # Schedule removal after exit
            print("  ⚠ Cannot remove while running from install directory")
            print(f"  Please manually delete: {install_dir}")
    except Exception as e:
        print(f"  ⚠ Could not remove files: {e}")
        print(f"  Please manually delete: {install_dir}")
    
    print()
    print("=" * 60)
    print("  ✓ UNINSTALL COMPLETE!")
    print("=" * 60)
    print()
    
    pause()
    return True

# ============================================================================
# MAIN
# ============================================================================

def main():
    # Check if running as uninstaller
    exe_name = os.path.basename(sys.executable).lower()
    
    if '--uninstall' in sys.argv or '-u' in sys.argv:
        run_uninstaller()
    elif 'uninstall' in exe_name:
        run_uninstaller()
    else:
        run_installer()

if __name__ == "__main__":
    main()


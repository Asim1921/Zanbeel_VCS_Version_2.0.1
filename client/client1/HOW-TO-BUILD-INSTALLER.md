# How to Build the FoxNest Windows Installer

This guide explains how to create a distributable Windows installer for FoxNest that works on any Windows PC.

## Quick Build (Recommended)

1. Open Command Prompt or PowerShell
2. Navigate to the `client` directory:
   ```cmd
   cd FoxNest-main\client
   ```
3. Run the build script:
   ```cmd
   build_all.bat
   ```
4. Find the installer at: `dist\FoxNest-Setup.exe`

## What Gets Created

After running `build_all.bat`, you'll have two files in the `dist` folder:

| File | Description | Use Case |
|------|-------------|----------|
| `fox.exe` | The Fox command-line client | For developers who want just the tool |
| `FoxNest-Setup.exe` | The full installer | For distributing to users |

## Manual Build Steps

If the batch script doesn't work, follow these steps manually:

### Step 1: Install Prerequisites

```cmd
py -m pip install pyinstaller requests
```

### Step 2: Build fox.exe

```cmd
py -m PyInstaller --onefile --name=fox --console fox.py
```

### Step 3: Build the Installer

```cmd
py -m PyInstaller --onefile --name=FoxNest-Setup --console --add-data "dist\fox.exe;." fox_simple_installer.py
```

## Distributing to Users

### For End Users

1. Copy `dist\FoxNest-Setup.exe` to the target computer
2. Double-click `FoxNest-Setup.exe`
3. Follow the installation prompts
4. Open a **NEW** Command Prompt or PowerShell
5. Type `fox --help` to verify installation

### What the Installer Does

1. Creates installation directory at `%LOCALAPPDATA%\FoxNest`
2. Copies `fox.exe` to the installation directory
3. Adds the directory to the user's PATH
4. Registers with Windows for easy uninstallation
5. Creates an uninstall script

### Uninstalling

Users can uninstall FoxNest by:
- Going to **Settings > Apps > Apps & features** and finding "FoxNest"
- Running the `uninstall.bat` in the installation directory
- Running `FoxNest-Setup.exe --uninstall`

## Troubleshooting

### "Python not found"

Install Python from https://python.org and make sure to check **"Add Python to PATH"** during installation.

### "fox.py not found"

Make sure you're running the build script from the `client` directory:
```cmd
cd FoxNest-main\client
build_all.bat
```

### Antivirus Warnings

Some antivirus software may flag PyInstaller-built executables. This is a false positive. You can:
- Add an exception for the `dist` folder
- Submit the file to your antivirus vendor for analysis
- The source code is fully available for inspection

### PATH Not Updated

After installation, you **must** open a new terminal window. The PATH change won't affect already-open terminals.

If `fox` still isn't recognized:
1. Open System Properties > Environment Variables
2. Check that the FoxNest directory is in your User PATH
3. Restart your computer if needed

## Alternative: Portable Installation

If you prefer not to use the installer:

1. Copy `dist\fox.exe` to any folder
2. Add that folder to your PATH manually
3. Or run `fox.exe` directly with its full path

## Build Requirements

- Windows 10 or later
- Python 3.8 or later
- PyInstaller (`pip install pyinstaller`)
- requests library (`pip install requests`)

## File Sizes

Typical file sizes after building:
- `fox.exe`: ~8-12 MB
- `FoxNest-Setup.exe`: ~15-20 MB (includes fox.exe)

The installer is larger because it bundles the fox.exe inside it.


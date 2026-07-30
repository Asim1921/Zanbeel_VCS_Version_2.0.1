# FoxNest Client Updater for Windows
# Downloads the latest fox.py directly from the FoxNest server via HTTP
# Automatically detects where fox.py needs to be placed so 'fox' command works.
# Run with:
#   powershell -ExecutionPolicy Bypass -File download-client-windows.ps1

$SERVER_URL = "http://192.168.15.207:33333"
$DOWNLOAD_URL = "$SERVER_URL/api/download/client"

Write-Host "FoxNest Client Updater" -ForegroundColor Cyan
Write-Host "======================" -ForegroundColor Cyan
Write-Host "Server : $SERVER_URL" -ForegroundColor Gray
Write-Host ""

# --- 1. Download to a temp file first ---
Write-Host "[1/4] Downloading updated client from server..." -ForegroundColor Yellow
$TEMP_PATH = Join-Path $env:TEMP "fox_update.py"
try {
    Invoke-WebRequest -Uri $DOWNLOAD_URL -OutFile $TEMP_PATH -UseBasicParsing -ErrorAction Stop
    $fileSize = (Get-Item $TEMP_PATH).Length
    Write-Host "      Downloaded $fileSize bytes" -ForegroundColor Green
} catch {
    Write-Host "      ERROR: Could not reach server at $SERVER_URL" -ForegroundColor Red
    Write-Host "      Make sure the FoxNest server is running and reachable." -ForegroundColor Yellow
    Write-Host "      Details: $_" -ForegroundColor Gray
    exit 1
}

# --- 2. Verify the download contains required command flags ---
Write-Host "[2/4] Verifying downloaded file..." -ForegroundColor Yellow
$content = Get-Content $TEMP_PATH -Raw
if (($content -match "rollback") -and ($content -match "--list-docs")) {
    Write-Host "      OK - fox.py contains rollback and selective docs flags." -ForegroundColor Green
} else {
    Write-Host "      WARNING - expected commands not found in downloaded file. Aborting." -ForegroundColor Red
    exit 1
}

# --- 3. Detect ALL locations where fox.py needs to go ---
Write-Host "[3/4] Detecting install locations..." -ForegroundColor Yellow

$destinations = @()

# Always update current directory
$destinations += $PWD.Path

# Find where the 'fox' command lives and trace its fox.py
$foxCmd = Get-Command fox -ErrorAction SilentlyContinue
if ($foxCmd) {
    $batPath = $foxCmd.Source
    Write-Host "      Found fox command: $batPath" -ForegroundColor Gray

    if ($batPath -match "\.exe$") {
        Write-Host "      NOTE: fox resolves to an .exe. Replacing fox.py alone may not update parser flags." -ForegroundColor Yellow
        Write-Host "      Reinstall/update fox.exe from latest installer if --list-docs is still missing." -ForegroundColor Yellow
    }

    # Read the bat to find where it expects fox.py
    $batContent = Get-Content $batPath -Raw -ErrorAction SilentlyContinue
    if ($batContent -match 'set FOX_PY=(.+)fox\.py') {
        # bat uses %SCRIPT_DIR%fox.py - means same dir as bat
        $batDir = Split-Path $batPath -Parent
        $destinations += $batDir
        Write-Host "      fox.bat expects fox.py in: $batDir" -ForegroundColor Gray
    }
}

# Deduplicate
$destinations = $destinations | Select-Object -Unique

# --- 4. Copy fox.py to all destinations ---
Write-Host "[4/4] Installing fox.py to all locations..." -ForegroundColor Yellow
$successCount = 0
foreach ($dir in $destinations) {
    $dest = Join-Path $dir "fox.py"
    try {
        # Backup existing
        if (Test-Path $dest) {
            Copy-Item $dest "$dest.backup" -Force -ErrorAction SilentlyContinue
        }
        Copy-Item $TEMP_PATH $dest -Force -ErrorAction Stop
        # Verify the copy actually took (WindowsApps can silently block)
        $verify = Get-Content $dest -Raw -ErrorAction SilentlyContinue
        if (($verify -match "rollback") -and ($verify -match "--list-docs")) {
            Write-Host "      [OK] $dest" -ForegroundColor Green
            $successCount++
        } else {
            Write-Host "      [BLOCKED] $dest (Windows protected this folder)" -ForegroundColor Yellow
        }
    } catch {
        Write-Host "      [FAILED] $dest - $_" -ForegroundColor Red
    }
}

Write-Host ""
Write-Host "======================" -ForegroundColor Cyan

if ($successCount -gt 0) {
    Write-Host "UPDATE COMPLETE! ($successCount location(s) updated)" -ForegroundColor Green
    Write-Host ""
    Write-Host "Test it:" -ForegroundColor White
    Write-Host "  fox rollback <commit-id>" -ForegroundColor Yellow
    Write-Host "  fox rollback <commit-id> --switch" -ForegroundColor Yellow
    Write-Host "  fox generate-docs --list-docs" -ForegroundColor Yellow
} else {
    Write-Host "INSTALL BLOCKED by Windows." -ForegroundColor Red
    Write-Host ""
    Write-Host "Run this instead to use the updated client directly:" -ForegroundColor White
    Write-Host "  python $TEMP_PATH rollback <commit-id>" -ForegroundColor Yellow
    Write-Host ""
    Write-Host "Or copy manually (run PowerShell as Administrator):" -ForegroundColor White
    Write-Host "  Copy-Item `"$TEMP_PATH`" `"$($foxCmd.Source -replace 'fox\.bat','fox.py')`" -Force" -ForegroundColor Yellow
}

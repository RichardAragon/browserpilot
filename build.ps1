# Build from the BrowserPilot source directory on Windows with Python 3.12.
# A fresh development environment is recommended. No account data is bundled.
param([string]$Python='python')
$ErrorActionPreference='Stop'
$sourceRoot = $PSScriptRoot
$buildRoot = Join-Path $sourceRoot 'build-work'
$releaseRoot = Join-Path $sourceRoot 'release'
& $Python -m pip install -r (Join-Path $sourceRoot 'requirements.txt') pyinstaller==6.22.3
if ($LASTEXITCODE -ne 0) { throw 'Dependency installation failed.' }
& $Python -m playwright install chromium --only-shell
if ($LASTEXITCODE -ne 0) { throw 'Browser installation failed.' }
$browserRoot = Join-Path $env:LOCALAPPDATA 'ms-playwright'
$headless = Get-ChildItem -LiteralPath $browserRoot -Directory | Where-Object Name -Like 'chromium_headless_shell-*' | Sort-Object LastWriteTime -Descending | Select-Object -First 1
if (-not $headless) { throw 'Playwright headless browser installation was not found.' }
& $Python -m PyInstaller --noconfirm --windowed --name BrowserPilot --icon (Join-Path $sourceRoot 'assets/browserpilot.ico') --distpath $releaseRoot --workpath $buildRoot --specpath $buildRoot --add-data "$(Join-Path $sourceRoot 'web');web" --add-data "$($headless.FullName);browsers/$($headless.Name)" --collect-all playwright (Join-Path $sourceRoot 'app.py')
if ($LASTEXITCODE -ne 0) { throw 'Build failed.' }
Copy-Item -LiteralPath (Join-Path $sourceRoot 'START_HERE.html') -Destination (Join-Path $releaseRoot 'BrowserPilot/START_HERE.html')
Write-Output 'Build complete. Keep BrowserPilot.exe beside its _internal folder.'

# Build MASON.exe on Windows from an unpacked MASON-<version>-windows-x64 folder (run from build\windows).
$ErrorActionPreference = "Stop"
$Root = Resolve-Path "$PSScriptRoot\..\.."
$Py = "$Root\runtime\python\python.exe"
& $Py -m pip install --quiet pyinstaller
& $Py -m PyInstaller --onefile --noconfirm --name MASON --console --distpath "$Root" --workpath "$env:TEMP\mason-build" --specpath "$env:TEMP\mason-build" "$Root\launcher\mason_exe_stub.py"
if (Test-Path "$Root\build\MASON.ico") { Write-Host "Add --icon build\MASON.ico to the PyInstaller line for a custom icon." }
Write-Host "Built $Root\MASON.exe"

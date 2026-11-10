# Local Lens installer: installs Python packages and adds Desktop + Start menu shortcuts.
# Usage (from the repo folder):  powershell -ExecutionPolicy Bypass -File install.ps1

$ErrorActionPreference = "Stop"
$here = Split-Path -Parent $MyInvocation.MyCommand.Path

$python = (Get-Command python -ErrorAction SilentlyContinue).Source
if (-not $python) { $python = (Get-Command py -ErrorAction SilentlyContinue).Source }
if (-not $python) {
    Write-Host "Python 3.9+ is required. Install it from https://www.python.org/downloads/ (tick 'Add to PATH')." -ForegroundColor Red
    exit 1
}

Write-Host "Installing packages..." -ForegroundColor Cyan
& $python -m pip install --upgrade --quiet --disable-pip-version-check -r (Join-Path $here "requirements.txt")
if ($LASTEXITCODE -ne 0) { Write-Host "pip install failed." -ForegroundColor Red; exit 1 }

$pythonw = & $python -c "import sys, os; print(os.path.join(os.path.dirname(sys.executable), 'pythonw.exe'))"
if (-not (Test-Path $pythonw)) { $pythonw = $python }

$ws = New-Object -ComObject WScript.Shell
$targets = @(
    [Environment]::GetFolderPath("Desktop"),
    (Join-Path $env:APPDATA "Microsoft\Windows\Start Menu\Programs")
)
foreach ($dir in $targets) {
    $lnk = $ws.CreateShortcut((Join-Path $dir "Local Lens.lnk"))
    $lnk.TargetPath = $pythonw
    $lnk.Arguments = "`"$(Join-Path $here 'local_lens.pyw')`""
    $lnk.WorkingDirectory = $here
    $lnk.IconLocation = Join-Path $here "local_lens.ico"
    $lnk.Description = "Grab text from any image - offline"
    $lnk.Save()
}

Write-Host "Done! Launch 'Local Lens' from your Desktop or Start menu." -ForegroundColor Green

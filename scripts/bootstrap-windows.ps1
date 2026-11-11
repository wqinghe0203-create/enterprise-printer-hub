$ErrorActionPreference = 'Stop'
$Root = Split-Path -Parent $PSScriptRoot
Set-Location -LiteralPath $Root

function Find-Python {
    $LocalPython = Join-Path $Root '.venv\Scripts\python.exe'
    if (Test-Path -LiteralPath $LocalPython -PathType Leaf) {
        try {
            & $LocalPython -c 'import sys; sys.exit(0 if sys.version_info >= (3,9) else 1)' 2>$null
            if ($LASTEXITCODE -eq 0) { return $LocalPython }
        } catch {}
    }
    $Candidates = @(
        (Join-Path $env:USERPROFILE '.workbuddy\binaries\python\envs\default\Scripts\python.exe'),
        (Join-Path $env:LOCALAPPDATA 'Programs\Python\Python312\python.exe')
    )
    if (Get-Command py -ErrorAction SilentlyContinue) {
        $Candidates += & py -3 -c 'import sys; print(sys.executable)' 2>$null
    }
    $Command = Get-Command python.exe -ErrorAction SilentlyContinue
    if ($Command) { $Candidates += $Command.Source }
    foreach ($Candidate in $Candidates) {
        if ($Candidate -and (Test-Path -LiteralPath $Candidate -PathType Leaf) -and $Candidate -notlike '*\Microsoft\WindowsApps\*') {
            try {
                & $Candidate -c 'import sys; sys.exit(0 if sys.version_info >= (3,9) else 1)' 2>$null
                if ($LASTEXITCODE -eq 0) { return $Candidate }
            } catch { continue }
        }
    }
}

try {
    $Python = Find-Python
    if (-not $Python) {
        $Winget = Get-Command winget.exe -ErrorAction SilentlyContinue
        if (-not $Winget) { throw 'Install Python from https://www.python.org/downloads/windows/ and retry.' }
        $Data = Join-Path $Root 'data'
        [void][IO.Directory]::CreateDirectory($Data)
        $LockPath = Join-Path $Data 'bootstrap-python.lock'
        $Deadline = [DateTime]::UtcNow.AddSeconds(3)
        while (-not $Lock) {
            try { $Lock = [IO.File]::Open($LockPath, 'OpenOrCreate', 'ReadWrite', 'None') }
            catch [IO.IOException] {
                if ([DateTime]::UtcNow -ge $Deadline) {
                    @{status='busy';phase='system_python';message='Python setup is still running.'} | ConvertTo-Json -Compress
                    exit 2
                }
                Start-Sleep -Milliseconds 250
            }
        }
        try {
            $Python = Find-Python
            if (-not $Python) {
                & $Winget.Source install --id Python.Python.3.12 --exact --source winget --scope user --disable-interactivity
                $Python = Find-Python
            }
        } finally { $Lock.Dispose() }
        if (-not $Python) { throw 'Python installation incomplete. Check winget output or install Python manually and retry.' }
    }
    & $Python (Join-Path $PSScriptRoot 'bootstrap.py') @args
    exit $LASTEXITCODE
} catch {
    @{status='failed';message=$_.Exception.Message} | ConvertTo-Json -Compress
    exit 1
}

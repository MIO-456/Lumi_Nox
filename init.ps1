# Standard verification entry point (Windows PowerShell 5.1+).
#
#   ./init.ps1              # compile + docs + tests
#   ./init.ps1 -Python py   # force a specific interpreter
#
# Every check here is offline: no network, no audio device, no game, no model.
# Native commands are judged by $LASTEXITCODE, not by stderr text: the test
# suite logs warnings to stderr on purpose and that is not a failure.
[CmdletBinding()]
param(
    [string]$Python = $env:PYTHON
)

Set-Location -LiteralPath $PSScriptRoot

if (-not $Python) {
    foreach ($candidate in @('python', 'py', 'python3')) {
        if (Get-Command $candidate -ErrorAction SilentlyContinue) { $Python = $candidate; break }
    }
}
if (-not $Python) {
    Write-Host 'init: no python found on PATH' -ForegroundColor Red
    exit 127
}

$steps = @(
    @{ Name = 'compile'; Args = @('-m', 'compileall', '-q', '.') },
    @{ Name = 'docs';    Args = @('scripts/check_docs.py') },
    @{ Name = 'tests';   Args = @('-m', 'unittest', 'discover', '-s', 'tests') }
)

Write-Host "== python: $(& $Python --version 2>&1) =="
foreach ($step in $steps) {
    Write-Host "== $($step.Name) =="
    & $Python @($step.Args)
    if ($LASTEXITCODE -ne 0) {
        Write-Host "init: $($step.Name) failed with exit $LASTEXITCODE" -ForegroundColor Red
        exit $LASTEXITCODE
    }
}

Write-Host '== all checks passed =='
Write-Host ''
Write-Host 'Next steps:'
Write-Host '  1. Read feature_list.json and pick ONE not-started feature'
Write-Host '  2. Read the doc routed by AGENTS.md section 4'
Write-Host '  3. Re-run this script before claiming done'

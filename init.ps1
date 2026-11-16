# Standard verification entry point (Windows PowerShell 5.1+).
#   ./init.ps1
#   ./init.ps1 -Python 'C:\Path With Spaces\python.exe'
# Checks use no cloud services, audio device, or game. Tests may use loopback.
[CmdletBinding()]
param(
    [string]$Python = $env:PYTHON
)

$ErrorActionPreference = 'Stop'
$exitCode = 1
$pushedLocation = $false
$previousUtf8 = [Environment]::GetEnvironmentVariable('PYTHONUTF8', 'Process')
$previousIoEncoding = [Environment]::GetEnvironmentVariable('PYTHONIOENCODING', 'Process')
$previousConsoleEncoding = [Console]::OutputEncoding

try {
    Push-Location -LiteralPath $PSScriptRoot
    $pushedLocation = $true
    $env:PYTHONUTF8 = '1'
    $env:PYTHONIOENCODING = 'utf-8'
    [Console]::OutputEncoding = [System.Text.UTF8Encoding]::new($false)

    if (-not $Python) {
        foreach ($candidate in @('python', 'py', 'python3')) {
            $command = Get-Command $candidate -CommandType Application -ErrorAction SilentlyContinue | Select-Object -First 1
            if ($command) { $Python = $command.Source; break }
        }
    }
    if (-not $Python) { throw 'no Python executable found on PATH' }
    # Resolve explicit paths too. PowerShell invocation errors do not reliably
    # change LASTEXITCODE, so a stale successful exit must never pass a check.
    $command = Get-Command $Python -CommandType Application -ErrorAction Stop | Select-Object -First 1
    $Python = $command.Source

    $steps = @(
        @{ Name = 'python';  Args = @('--version') },
        @{ Name = 'compile'; Args = @('-m', 'compileall', '-q', '.') },
        @{ Name = 'docs';    Args = @('scripts/check_docs.py') },
        @{ Name = 'tests';   Args = @('-m', 'unittest', 'discover', '-s', 'tests') }
    )
    foreach ($step in $steps) {
        Write-Host "== $($step.Name) =="
        # Do not redirect native stderr into PowerShell's error stream: tests
        # intentionally write warnings there. Check invocation and exit status.
        & $Python @($step.Args)
        $invoked = $?
        $nativeExit = $LASTEXITCODE
        if (-not $invoked -or $nativeExit -ne 0) {
            if ($nativeExit -is [int] -and $nativeExit -ne 0) { $exitCode = $nativeExit }
            throw "$($step.Name) failed (exit $nativeExit)"
        }
    }

    Write-Host '== all checks passed =='
    Write-Host 'Continue the agreed task; use AGENTS.md and docs/README.md for relevant guidance.'
    $exitCode = 0
}
catch {
    Write-Host "init: $($_.Exception.Message)" -ForegroundColor Red
}
finally {
    [Environment]::SetEnvironmentVariable('PYTHONUTF8', $previousUtf8, 'Process')
    [Environment]::SetEnvironmentVariable('PYTHONIOENCODING', $previousIoEncoding, 'Process')
    [Console]::OutputEncoding = $previousConsoleEncoding
    if ($pushedLocation) { Pop-Location }
}
exit $exitCode

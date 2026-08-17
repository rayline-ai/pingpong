# Thin wrapper. `pingpong up|down|logs` drive compose; anything else is passed
# straight to the CLI inside the running API container.
[CmdletBinding()]
param([Parameter(ValueFromRemainingArguments = $true)][string[]]$Args)

$ErrorActionPreference = 'Stop'
Push-Location $PSScriptRoot
try {
    $rest = @()
    if ($Args.Count -gt 1) { $rest = $Args[1..($Args.Count - 1)] }

    switch ($Args[0]) {
        'up'   { docker compose up -d --build @rest }
        'down' { docker compose down @rest }
        'logs' {
            if ($rest.Count -eq 0) { $rest = @('api') }
            docker compose logs -f @rest
        }
        { $_ -in @($null, '', '-h', '--help') } {
            Write-Host 'usage: pingpong up|down|logs'
            Write-Host '       pingpong doctor'
            Write-Host '       pingpong round owner/repo#123'
        }
        default { docker compose exec api python -m src.cli @Args }
    }
    exit $LASTEXITCODE
}
finally { Pop-Location }

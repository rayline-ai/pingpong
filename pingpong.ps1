# Thin wrapper. `pingpong up|down|logs` drive compose, `accounts`, `model` and
# `onboard` run on the host; anything else is passed straight to the CLI inside
# the running API container.
[CmdletBinding()]
param([Parameter(ValueFromRemainingArguments = $true)][string[]]$Args)

$ErrorActionPreference = 'Stop'
Push-Location $PSScriptRoot
try {
    $rest = @()
    if ($Args.Count -gt 1) { $rest = $Args[1..($Args.Count - 1)] }

    switch ($Args[0]) {
        'up'   {
            # Before compose reads .env: Forgejo bakes FORGEJO_ROOT_URL into
            # clone URLs at boot, so the address has to be right first.
            # Best-effort — no python is not a reason not to start the stack.
            $python = $null
            foreach ($py in @('python', 'python3', 'py')) {
                $found = Get-Command $py -ErrorAction SilentlyContinue
                if ($found) { $python = $found.Source; break }
            }
            if ($python -and (Test-Path '.env')) { & $python -m src.address }
            # This one does refuse. No brain is chosen out of the box, and
            # starting anyway buys nothing: the stack comes up and dies at the
            # first review, the furthest possible place from the cause.
            if ($python) {
                & $python -m src.models --check
                if ($LASTEXITCODE -ne 0) { exit 1 }
            }
            docker compose up -d --build @rest
        }
        'down' { docker compose down @rest }
        'logs' {
            if ($rest.Count -eq 0) { $rest = @('api') }
            docker compose logs -f @rest
        }
        { $_ -in @('accounts', 'model', 'onboard') } {
            # Host-side: they need this machine's folder, ~/.netrc, .env and
            # `docker compose exec`, none of which the API container can see.
            # Git Bash ships with Git for Windows, which anyone cloning this
            # already has.
            $script = "./$($Args[0]).sh"
            $bash = Get-Command bash -ErrorAction SilentlyContinue
            if (-not $bash) {
                Write-Error "$($Args[0]) needs bash. Install Git for Windows, or run $script from Git Bash / WSL."
                exit 1
            }
            & $bash.Source $script @rest
        }
        { $_ -in @($null, '', '-h', '--help') } {
            Write-Host 'usage: pingpong up|down|logs'
            Write-Host '       pingpong accounts [--user someone@example.com]'
            Write-Host '       pingpong model [reviewer|coder <endpoint> <model>]'
            Write-Host '       pingpong onboard ../some-repo'
            Write-Host '       pingpong doctor'
            Write-Host '       pingpong round owner/repo#123'
        }
        default { docker compose exec api python -m src.cli @Args }
    }
    exit $LASTEXITCODE
}
finally { Pop-Location }

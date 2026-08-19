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

    # AGENT_MODE decides both which compose files to use and which pre-flight
    # check applies, and both happen before compose runs — so this reads .env
    # rather than letting compose interpolate it.
    $mode = 'router'
    if (Test-Path '.env') {
        $line = Select-String -Path '.env' -Pattern '^AGENT_MODE=' | Select-Object -Last 1
        if ($line) { $mode = $line.Line -replace '^AGENT_MODE=\s*', '' -replace '["'']', '' }
    }
    if (-not $mode) { $mode = 'router' }
    $subscription = $mode -in @('claude-sub', 'codex-sub')

    # A second -f replaces the default list rather than adding to it, so the base
    # file is named explicitly. One overlay per subscription mode: claude-sub
    # mounts the host's login, codex-sub deliberately mounts nothing from the
    # host and puts Hermes' own session on a volume.
    $files = @('-f', 'docker-compose.yml')
    if ($subscription) { $files += @('-f', "docker-compose.$mode.yml") }

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
            # These two do refuse, and only one of them applies. A brain has to
            # come from somewhere and starting without one buys nothing: the
            # stack comes up and dies at the first review, the furthest possible
            # place from the cause.
            if ($python) {
                if ($subscription) { & $python -m src.subscription }
                else               { & $python -m src.models --check }
                if ($LASTEXITCODE -ne 0) { exit 1 }
            }
            docker compose @files up -d --build @rest
        }
        'down' { docker compose @files down @rest }
        'logs' {
            if ($rest.Count -eq 0) { $rest = @('api') }
            docker compose @files logs -f @rest
        }
        { $_ -in @('accounts', 'model', 'onboard', 'login') } {
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
            Write-Host '       pingpong login              (AGENT_MODE=codex-sub only)'
            Write-Host '       pingpong onboard ../some-repo'
            Write-Host '       pingpong doctor'
            Write-Host '       pingpong round owner/repo#123'
        }
        default { docker compose @files exec api python -m src.cli @Args }
    }
    exit $LASTEXITCODE
}
finally { Pop-Location }

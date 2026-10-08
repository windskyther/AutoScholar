param([switch]$KeepStack)
$ErrorActionPreference = 'Stop'
# Disable inherited system HTTP proxy only in this test helper process.
[System.Net.WebRequest]::DefaultWebProxy = $null
$repoRoot = (Resolve-Path (Join-Path $PSScriptRoot '..')).Path
$project = 'autoscholar-phase9g-' + [guid]::NewGuid().ToString('N').Substring(0, 8)
if ($project -notmatch '^autoscholar-phase9g-[a-f0-9]{8}$') { throw 'Unsafe test project name' }
$composeFile = Join-Path $repoRoot 'tests\integration\phase9.compose.yaml'
$publicConfig = Join-Path $repoRoot 'tests\integration\phase9-public-config.txt'
$compose = @('compose', '--env-file', $publicConfig, '-f', $composeFile, '-p', $project)
function Invoke-TestCompose {
    # A simple function deliberately has no common -Debug parameter: otherwise
    # PowerShell consumes Docker's -d flag instead of forwarding it.
    & docker @compose @args
    if ($LASTEXITCODE -ne 0) { throw ('Isolated Compose failed: ' + ($args -join ' ')) }
}
if (Get-NetTCPConnection -LocalPort 18019,5183 -State Listen -ErrorAction SilentlyContinue) {
    throw 'Ports 18019/5183 are in use; existing services will not be stopped.'
}
& docker volume inspect autoscholar_mnist_data --format '{{.Name}}'
if ($LASTEXITCODE -ne 0) { throw 'Public MNIST source volume is required; no automatic dataset download.' }
if (& docker ps -aq --filter "label=com.docker.compose.project=$project") { throw 'Test project already exists' }
Write-Host "Independent test project: $project (no external API calls)"
$oldLocation = Get-Location
try {
    Set-Location $repoRoot
    Invoke-TestCompose config --quiet
    Invoke-TestCompose build api
    Invoke-TestCompose up -d --wait postgres redis qdrant
    Invoke-TestCompose run --rm --no-deps migrate
    Invoke-TestCompose run --rm --no-deps migrate python /acceptance/runtime.py database-checks
    # No consumers are running during schema rollback/forward validation.
    Invoke-TestCompose run --rm --no-deps migrate alembic downgrade 20261005_0012
    Invoke-TestCompose run --rm --no-deps migrate alembic upgrade head
    Invoke-TestCompose run --rm --no-deps migrate python /acceptance/runtime.py backfill-check
    Invoke-TestCompose run --rm --no-deps dataset-copy
    Invoke-TestCompose up -d --no-deps sandbox-manager api ingress workflow-worker document-worker
    $ready = $false
    $lastHealthProblem = 'No response'
    $deadline = [DateTime]::UtcNow.AddSeconds(90)
    while ([DateTime]::UtcNow -lt $deadline) {
        try {
            $health = Invoke-RestMethod -Uri 'http://127.0.0.1:18019/workbench/health/ready' -Headers @{Authorization='Bearer public-phase9g-token'} -TimeoutSec 2
            if ($health.status -eq 'ready') { $ready = $true; break }
        } catch { $lastHealthProblem = $_.Exception.Message }
        Start-Sleep -Milliseconds 250
    }
    if (-not $ready) { Invoke-TestCompose logs --tail 40 api workflow-worker document-worker; throw "Isolated API not ready: $lastHealthProblem" }
    Set-Location (Join-Path $repoRoot 'web')
    & npm run build
    if ($LASTEXITCODE -ne 0) { throw 'Frontend build failed' }
    & node scripts/browser.mjs test --config playwright.stack.config.ts
    if ($LASTEXITCODE -ne 0) { throw 'Independent full-stack browser acceptance failed' }
    Invoke-TestCompose restart api workflow-worker document-worker
    $deadline = [DateTime]::UtcNow.AddSeconds(30)
    $restarted = $false
    while ([DateTime]::UtcNow -lt $deadline) {
        try {
            $health = Invoke-RestMethod -Uri 'http://127.0.0.1:18019/workbench/health/ready' -Headers @{Authorization='Bearer public-phase9g-token'} -TimeoutSec 2
            if ($health.status -eq 'ready') { $restarted = $true; break }
        } catch { }
        Start-Sleep -Milliseconds 250
    }
    if (-not $restarted) { throw 'Isolated API did not recover after restart' }
    Invoke-TestCompose run --rm --no-deps migrate python /acceptance/runtime.py persistence-check
    Write-Host 'Phase 9 independent stack acceptance passed; external API calls: 0'
} finally {
    Set-Location $oldLocation
    if ($KeepStack) {
        Write-Host "Stack retained for inspection: $project. It uses port 18019, public test credentials only."
    } else {
        # Exact random project only; external public MNIST is never removed.
        $containers = & docker ps -aq --filter "label=com.docker.compose.project=$project"
        foreach ($container in $containers) {
            $labels = (& docker inspect --format '{{json .Config.Labels}}' $container) | ConvertFrom-Json
            $owner = $labels.'com.docker.compose.project'
            if ($owner -ne $project) { throw 'Cleanup ownership mismatch' }
        }
        Invoke-TestCompose down --volumes --remove-orphans --timeout 15
        Write-Host 'Removed independent test containers/volumes; normal services and public MNIST source retained.'
    }
}

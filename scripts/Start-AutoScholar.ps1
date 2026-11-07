[CmdletBinding()]
param(
    [switch]$Rebuild,
    [switch]$NoBrowser
)

$ErrorActionPreference = 'Stop'
$repoRoot = (Resolve-Path -LiteralPath (Join-Path $PSScriptRoot '..')).Path
$webRoot = Join-Path $repoRoot 'web'
$runtimeRoot = Join-Path $repoRoot 'data\launcher'
$webUrl = 'http://127.0.0.1:5173'
$apiUrl = 'http://127.0.0.1:8000'
$startupLock = $null
$webProcess = $null
$started = $false
$oldLocation = Get-Location

function Read-LocalResponse {
    param([string]$Url)
    $request = [System.Net.HttpWebRequest]::Create($Url)
    $request.Proxy = $null
    $request.AllowAutoRedirect = $false
    $request.Timeout = 2000
    $request.ReadWriteTimeout = 2000
    $response = $null
    try {
        $response = $request.GetResponse()
        $reader = [System.IO.StreamReader]::new($response.GetResponseStream())
        try { return @{ Status = [int]$response.StatusCode; Body = $reader.ReadToEnd() } }
        finally { $reader.Dispose() }
    } catch [System.Net.WebException] {
        $response = $_.Exception.Response
        if ($response) { return @{ Status = [int]$response.StatusCode; Body = '' } }
        return @{ Status = 0; Body = '' }
    } finally {
        if ($response) { $response.Dispose() }
    }
}

function Test-WebPort {
    $client = [System.Net.Sockets.TcpClient]::new()
    try { return $client.ConnectAsync('127.0.0.1', 5173).Wait(500) }
    catch { return $false }
    finally { $client.Dispose() }
}

function Get-DockerType {
    try {
        $value = & $script:dockerBinary info --format '{{.OSType}}' 2>$null
        if ($LASTEXITCODE -eq 0) { return ($value -join '').Trim() }
    } catch { return '' }
    return ''
}

try {
    Set-Location -LiteralPath $repoRoot
    if (-not (Test-Path -LiteralPath '.env' -PathType Leaf)) {
        Copy-Item -LiteralPath '.env.example' -Destination '.env'
        throw 'Created .env from the example. Fill in the model encryption key and sandbox credential, then start again. See README.md.'
    }
    $nodeCommand = Get-Command node.exe -ErrorAction SilentlyContinue
    $npmCommand = Get-Command npm.cmd -ErrorAction SilentlyContinue
    if (-not $nodeCommand -or -not $npmCommand) { throw 'Install Node.js 24.x, then start again.' }
    $nodeBinary = $nodeCommand.Source
    $nodeVersion = & $nodeBinary --version
    if ($LASTEXITCODE -ne 0 -or $nodeVersion -notmatch '^v24\.') { throw 'AutoScholar requires Node.js 24.x.' }
    $dockerCommand = Get-Command docker.exe -ErrorAction SilentlyContinue
    $script:dockerBinary = 'D:\Applications\Docker\resources\bin\docker.exe'
    if (-not (Test-Path -LiteralPath $script:dockerBinary)) {
        if (-not $dockerCommand) { throw 'Docker Desktop is required. Install it before starting AutoScholar.' }
        $script:dockerBinary = $dockerCommand.Source
    }
    New-Item -ItemType Directory -Path $runtimeRoot -Force | Out-Null
    try {
        $startupLock = [System.IO.File]::Open((Join-Path $runtimeRoot 'startup.lock'), 'OpenOrCreate', 'ReadWrite', 'None')
    } catch [System.IO.IOException] {
        Write-Host 'AutoScholar startup is already running. Wait for that window to finish.'
        exit 0
    }
    $composeArguments = @('compose', '--project-directory', $repoRoot, '--env-file', (Join-Path $repoRoot '.env'), '-f', (Join-Path $repoRoot 'compose.yaml'))
    & $script:dockerBinary @composeArguments version --short
    if ($LASTEXITCODE -ne 0) { throw 'Docker Compose is unavailable. Update Docker Desktop.' }
    # Capture configuration privately: never print or save the expanded secrets.
    $configurationText = & $script:dockerBinary @composeArguments config --format json
    if ($LASTEXITCODE -ne 0) { throw 'Cannot load .env or compose.yaml.' }
    try { $configuration = ($configurationText -join "`n") | ConvertFrom-Json }
    catch { throw 'Cannot read the Compose configuration.' }
    $modelKey = [string]$configuration.services.api.environment.MODEL_ENCRYPTION_KEY
    $sandboxToken = [string]$configuration.services.api.environment.SANDBOX_MANAGER_TOKEN
    if ([string]::IsNullOrWhiteSpace($modelKey)) { throw 'Set MODEL_ENCRYPTION_KEY in .env first. See README.md.' }
    if ($sandboxToken.Length -lt 32 -or $sandboxToken -eq $modelKey) {
        throw 'Set SANDBOX_MANAGER_TOKEN of at least 32 characters in .env. See README.md.'
    }
    $configurationText = $null
    $configuration = $null
    $sandboxToken = $null

    Write-Host '[1/3] Preparing Docker...'
    $dockerType = Get-DockerType
    if (-not $dockerType) {
        $desktopCandidates = @(
            'D:\Applications\Docker\Docker Desktop.exe',
            [System.IO.Path]::GetFullPath((Join-Path (Split-Path $script:dockerBinary -Parent) '..\..\Docker Desktop.exe')),
            (Join-Path $env:ProgramFiles 'Docker\Docker\Docker Desktop.exe')
        )
        $desktopBinary = $desktopCandidates | Where-Object { Test-Path -LiteralPath $_ -PathType Leaf } | Select-Object -First 1
        if (-not $desktopBinary) { throw 'Start Docker Desktop, then run this launcher again.' }
        Start-Process -FilePath $desktopBinary -WindowStyle Hidden | Out-Null
        $deadline = [DateTime]::UtcNow.AddSeconds(180)
        do {
            Start-Sleep -Seconds 2
            $dockerType = Get-DockerType
        } while (-not $dockerType -and [DateTime]::UtcNow -lt $deadline)
        if (-not $dockerType) { throw 'Docker did not become ready within 3 minutes. Check Docker Desktop and try again.' }
    }
    if ($dockerType -ne 'linux') { throw 'Switch Docker Desktop to Linux containers, then start again.' }

    Write-Host '[2/3] Starting background services (first launch may build images)...'
    $upArguments = @('up', '-d', '--wait', '--wait-timeout', '180')
    if ($Rebuild) { $upArguments += '--build' }
    & $script:dockerBinary @composeArguments @upArguments
    if ($LASTEXITCODE -ne 0) { throw 'Background services did not become ready. Run docker compose ps -a to inspect them.' }
    $session = Read-LocalResponse -Url ($apiUrl + '/workbench/session')
    if ($session.Status -ne 200) { throw 'The workbench API is unavailable. Check .env; after code updates use Start-AutoScholar.cmd -Rebuild.' }

    Write-Host '[3/3] Preparing the workbench...'
    if (Test-WebPort) {
        $page = Read-LocalResponse -Url $webUrl
        if ($page.Status -ne 200 -or $page.Body -notmatch '<title>AutoScholar') {
            throw 'Port 5173 is occupied by another service. Close that service or check the existing workbench.'
        }
        Write-Host 'Using the workbench that is already running.'
    } else {
        $lockPath = Join-Path $webRoot 'package-lock.json'
        $lockHash = (Get-FileHash -LiteralPath $lockPath -Algorithm SHA256).Hash
        $installedHashPath = Join-Path $runtimeRoot 'web-dependencies.sha256'
        $installedHash = ''
        if (Test-Path -LiteralPath $installedHashPath) { $installedHash = (Get-Content -LiteralPath $installedHashPath -Raw).Trim() }
        $vitePath = Join-Path $webRoot 'node_modules\vite\bin\vite.js'
        if (-not (Test-Path -LiteralPath $vitePath) -or $installedHash -ne $lockHash) {
            Write-Host 'Installing workbench dependencies...'
            Push-Location -LiteralPath $webRoot
            try {
                & $npmCommand.Source ci --no-audit --no-fund
                if ($LASTEXITCODE -ne 0) { throw 'Workbench dependency installation failed. Check npm output and your network.' }
            } finally { Pop-Location }
            Set-Content -LiteralPath $installedHashPath -Value $lockHash -Encoding ASCII
        }
        $logName = 'workbench-' + [DateTime]::UtcNow.ToString('yyyyMMdd-HHmmss-fff')
        $previousApiPort = $env:WORKBENCH_API_PORT
        try {
            $env:WORKBENCH_API_PORT = '8000'
            $webProcess = Start-Process -FilePath $nodeBinary -ArgumentList @(('"' + $vitePath + '"'), '--host', '127.0.0.1', '--port', '5173', '--strictPort') -WorkingDirectory $webRoot -WindowStyle Hidden -RedirectStandardOutput (Join-Path $runtimeRoot ($logName + '.log')) -RedirectStandardError (Join-Path $runtimeRoot ($logName + '.error.log')) -PassThru
        } finally { $env:WORKBENCH_API_PORT = $previousApiPort }
        $deadline = [DateTime]::UtcNow.AddSeconds(45)
        do {
            $page = Read-LocalResponse -Url $webUrl
            if ($page.Status -eq 200 -and $page.Body -match '<title>AutoScholar') { break }
            if ($webProcess.HasExited) { throw ('Workbench exited. See logs in ' + $runtimeRoot) }
            Start-Sleep -Milliseconds 500
        } while ([DateTime]::UtcNow -lt $deadline)
        if ($page.Status -ne 200 -or $page.Body -notmatch '<title>AutoScholar') { throw ('Workbench did not become ready. See logs in ' + $runtimeRoot) }
        @{ pid = $webProcess.Id; url = $webUrl; started_at = [DateTimeOffset]::UtcNow.ToString('o') } | ConvertTo-Json | Set-Content -LiteralPath (Join-Path $runtimeRoot 'workbench.json') -Encoding ASCII
    }
    $proxySession = Read-LocalResponse -Url ($webUrl + '/api/workbench/session')
    if ($proxySession.Status -ne 200) { throw 'The existing workbench cannot reach this API. Close its npm/Vite process, then start again.' }
    $started = $true
    Write-Host ('Ready: ' + $webUrl)
    Write-Host 'The local workbench connects automatically. You can close this launcher window.'
    if (-not $NoBrowser) { Start-Process -FilePath $webUrl | Out-Null }
} catch {
    Write-Host ('Startup failed: ' + $_.Exception.Message) -ForegroundColor Red
    exit 1
} finally {
    $modelKey = $null
    if (-not $started -and $webProcess -and -not $webProcess.HasExited) { Stop-Process -Id $webProcess.Id -ErrorAction SilentlyContinue }
    if ($startupLock) { $startupLock.Dispose() }
    Set-Location -LiteralPath $oldLocation.Path
}

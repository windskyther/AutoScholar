[CmdletBinding()]
param([string]$Toolchain = '')
$ErrorActionPreference = 'Stop'
$root = Split-Path (Split-Path $PSScriptRoot -Parent) -Parent
$desktop = Join-Path $root 'desktop'
$target = Join-Path $desktop 'target'
$runtime = Join-Path $desktop 'runtime'
New-Item -ItemType Directory -Force $target,$runtime | Out-Null

function Install-VerifiedArchive($uri, $hash, $destination, $name) {
    $archive = Join-Path $target $name
    if (!(Test-Path $archive) -or (Get-FileHash $archive).Hash -ne $hash) {
        Invoke-WebRequest -UseBasicParsing $uri -OutFile $archive
    }
    if ((Get-FileHash $archive).Hash -ne $hash) { throw "Checksum mismatch for $name" }
    Expand-Archive -LiteralPath $archive -DestinationPath $destination -Force
}
Install-VerifiedArchive 'https://github.com/qdrant/qdrant/releases/download/v1.19.0/qdrant-x86_64-pc-windows-msvc.zip' '980cb2e1ae771155cf211da8c0a8a9206b6482bd4effdc4db994d3adb707b087' $runtime 'qdrant.zip'
Install-VerifiedArchive 'https://github.com/tauri-apps/binary-releases/releases/download/nsis-3.11/nsis-3.11.zip' 'c7d27f780ddb6cffb4730138cd1591e841f4b7edb155856901cdf5f214394fa1' (Join-Path $target 'tools') 'tauri-nsis.zip'
Push-Location $root
try {
    uv sync --locked --all-groups
    if ($LASTEXITCODE) { throw 'Python dependency build failed' }
    npm.cmd --prefix frontend ci
    if ($LASTEXITCODE) { throw 'Frontend dependency build failed' }
    npm.cmd --prefix frontend run build
    if ($LASTEXITCODE) { throw 'Frontend build failed' }
    $toolArgs = @()
    if ($Toolchain) { $toolArgs += "+$Toolchain" }
    cargo @toolArgs build --manifest-path desktop/Cargo.toml --release --locked
    if ($LASTEXITCODE) { throw 'Desktop shell build failed' }
    $cargoTarget = if ($env:CARGO_TARGET_DIR) { $env:CARGO_TARGET_DIR } else { $target }
    & .venv/Scripts/python.exe desktop/scripts/build_desktop.py --shell (Join-Path $cargoTarget 'release/autoscholar-desktop.exe') --makensis (Join-Path $target 'tools/nsis-3.11/makensis.exe')
    if ($LASTEXITCODE) { throw 'Desktop installer build failed' }
} finally { Pop-Location }

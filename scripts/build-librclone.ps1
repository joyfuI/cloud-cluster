[CmdletBinding()]
param([string]$SourceDir, [string]$Compiler = 'C:\msys64\mingw64\bin\gcc.exe')
$ErrorActionPreference = 'Stop'
$projectRoot = Split-Path $PSScriptRoot -Parent
$tag = 'v1.76.0'
$commit = 'e982ff36668f5130d94b4f70409babb3d964df42'
if (-not $SourceDir) { $SourceDir = Join-Path $projectRoot "build\rclone-$tag" }
if (-not (Test-Path -LiteralPath $SourceDir)) {
    git clone --depth 1 --branch $tag https://github.com/rclone/rclone.git $SourceDir
    if ($LASTEXITCODE) { throw 'rclone source download failed' }
}
$SourceDir = (Resolve-Path -LiteralPath $SourceDir).Path
$actual = git -C $SourceDir rev-parse HEAD
if ($LASTEXITCODE -or $actual -ne $commit) { throw 'Pinned rclone source commit does not match' }
if (-not (Test-Path -LiteralPath $Compiler)) { throw 'Install a MinGW-compatible GCC compiler first' }
$nativeDir = Join-Path $projectRoot 'src\cloud_cluster\_native'
$cacheDir = Join-Path $projectRoot 'build\go-cache'
$moduleDir = Join-Path $projectRoot 'build\go-mod'
New-Item -ItemType Directory -Force -Path $nativeDir,$cacheDir,$moduleDir | Out-Null
$overlayPath = Join-Path $projectRoot 'build\rclone-overlay.json'
$overlay = @{}
$overlay[(Join-Path $SourceDir 'librclone\cloudcluster_rename.go')] = Join-Path $PSScriptRoot 'rclone-rename-dir.go'
[IO.File]::WriteAllText($overlayPath, (@{ Replace = $overlay } | ConvertTo-Json))
$env:CGO_ENABLED = '1'
$env:GOOS = 'windows'
$env:GOARCH = 'amd64'
$env:CC = $Compiler
$env:GOCACHE = $cacheDir
$env:GOMODCACHE = $moduleDir
Push-Location $SourceDir
try {
    go build -p 4 -overlay $overlayPath -buildmode=c-shared -trimpath "-ldflags=-s -w -X github.com/rclone/rclone/fs.Version=$tag" -o (Join-Path $nativeDir 'librclone.dll') ./librclone
    if ($LASTEXITCODE) { throw 'librclone build failed' }
    $modules = go version -m (Join-Path $nativeDir 'librclone.dll')
    if ($LASTEXITCODE) { throw 'Go module manifest failed' }
    $dllHash = (Get-FileHash -LiteralPath (Join-Path $nativeDir 'librclone.dll') -Algorithm SHA256).Hash
    $licenseDir = Join-Path $projectRoot "build\rclone-licenses\$dllHash"
    New-Item -ItemType Directory -Force -Path $licenseDir | Out-Null
    $modules | Set-Content -LiteralPath (Join-Path $licenseDir 'modules.txt') -Encoding utf8
    $records = [regex]::Matches(($modules -join "`n"), '(?m)^\s+dep\s+(\S+)\s+(\S+)')
    foreach ($record in $records) {
        $moduleName = $record.Groups[1].Value + '@' + $record.Groups[2].Value
        $escaped = [regex]::Replace($moduleName, '[A-Z]', { param($match) '!' + $match.Value.ToLowerInvariant() })
        $sourceModule = Join-Path $moduleDir $escaped
        if (-not (Test-Path -LiteralPath $sourceModule)) { throw "Module license source missing: $moduleName" }
        $destination = Join-Path $licenseDir ($moduleName -replace '[/\\:]','_')
        foreach ($file in (Get-ChildItem -LiteralPath $sourceModule -File | Where-Object Name -Match '^(LICENSE|LICENCE|COPYING|NOTICE)')) {
            New-Item -ItemType Directory -Force -Path $destination | Out-Null
            Copy-Item -LiteralPath $file.FullName -Destination $destination -Force
        }
    }
    $info = [ordered]@{ rclone=$tag; commit=$actual; go=(go version); gcc=(& $Compiler --version | Select-Object -First 1); sha256=$dllHash }
    $info | ConvertTo-Json | Set-Content -LiteralPath (Join-Path $nativeDir 'build-info.json') -Encoding utf8
    $info | ConvertTo-Json
} finally { Pop-Location }

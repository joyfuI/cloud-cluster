[CmdletBinding()]
param([string]$Python)
$ErrorActionPreference = 'Stop'
$projectRoot = Split-Path $PSScriptRoot -Parent
if (-not $Python) { $Python = Join-Path $projectRoot '.venv\Scripts\python.exe' }
$nativeDir = Join-Path $projectRoot 'src\cloud_cluster\_native'
$dll = Join-Path $nativeDir 'librclone.dll'
$manifest = Join-Path $nativeDir 'build-info.json'
if (-not (Test-Path -LiteralPath $dll) -or -not (Test-Path -LiteralPath $manifest)) {
    throw 'Run scripts/build-librclone.ps1 first'
}
$buildInfo = Get-Content -Raw -LiteralPath $manifest | ConvertFrom-Json
$dllHash = (Get-FileHash -LiteralPath $dll -Algorithm SHA256).Hash
$moduleNotices = Join-Path $projectRoot "build\rclone-licenses\$dllHash"
if ($buildInfo.sha256 -ne $dllHash -or -not (Test-Path -LiteralPath (Join-Path $moduleNotices 'modules.txt'))) {
    throw 'DLL and module notices must come from the same build. Run scripts/build-librclone.ps1.'
}
Push-Location $projectRoot
$previousResourceMode = $env:NUITKA_RESOURCE_MODE
try {
    # A fresh folder prevents local configs left beside an old payload from entering the exe.
    $packageBuild = Join-Path $projectRoot ('build\package-' + [guid]::NewGuid().ToString('N'))
    $qtTranslation = & $Python -c "from pathlib import Path; from PySide6.QtCore import QLibraryInfo; print(Path(QLibraryInfo.path(QLibraryInfo.LibraryPath.TranslationsPath)) / 'qtbase_ko.qm')"
    if ($LASTEXITCODE -or -not (Test-Path -LiteralPath $qtTranslation)) {
        throw 'Qt Korean translation file is missing'
    }
    $appIcon = 'src/cloud_cluster/cloud-cluster.ico'
    $dataOptions = @(
        '--include-data-dir=licenses=licenses'
        "--include-data-dir=$moduleNotices=licenses/go-modules"
        "--include-data-files=$appIcon=cloud_cluster/cloud-cluster.ico"
        '--include-data-files=src/cloud_cluster/_native/build-info.json=rclone-build-info.json'
        '--include-data-files=scripts/rclone-rename-dir.go=licenses/rclone-rename-dir.go'
        "--include-data-files=$qtTranslation=PySide6/translations/qtbase_ko.qm"
    )
    # Avoid stale constants and payloads from Zig's C23 #embed compilation cache.
    $env:NUITKA_RESOURCE_MODE = 'coff_obj'
    # Keep a runnable payload so users can run it with replacement Qt libraries after extraction.
    & $Python -m nuitka --onefile --onefile-no-dll --enable-plugin=pyside6 --windows-console-mode=disable --windows-icon-from-ico=$appIcon --output-filename=CloudCluster.exe --output-dir=$packageBuild --user-package-configuration-file=nuitka-package.config.yml @dataOptions --assume-yes-for-downloads --zig --python-flag=-m src/cloud_cluster
    if ($LASTEXITCODE) { throw 'Nuitka deployment failed' }
    $packageDir = Join-Path $projectRoot 'dist'
    New-Item -ItemType Directory -Path $packageDir -Force | Out-Null
    Copy-Item -LiteralPath (Join-Path $packageBuild 'CloudCluster.exe') -Destination (Join-Path $packageDir 'CloudCluster.exe') -Force
} finally {
    $env:NUITKA_RESOURCE_MODE = $previousResourceMode
    Pop-Location
}

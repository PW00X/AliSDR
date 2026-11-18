param(
    [Parameter(Mandatory = $true)]
    [string]$OutputZip,
    [ValidatePattern('^3\.13\.\d+$')]
    [string]$PythonVersion = '3.13.17'
)

$ErrorActionPreference = 'Stop'
$repo = (Resolve-Path (Join-Path $PSScriptRoot '../..')).Path
$output = $ExecutionContext.SessionState.Path.GetUnresolvedProviderPathFromPSPath($OutputZip)
if (Test-Path -LiteralPath $output) { throw "Output already exists: $output" }

Add-Type -AssemblyName System.IO.Compression.FileSystem
$tempRoot = (Resolve-Path -LiteralPath ([IO.Path]::GetTempPath())).Path
$temp = Join-Path $tempRoot ("alisdr-worker-" + [guid]::NewGuid().ToString('N'))
$bundle = Join-Path $temp 'bundle'
New-Item -ItemType Directory -Path $bundle -Force | Out-Null
try {
    $package = Join-Path $temp 'python.nupkg'
    Invoke-WebRequest -UseBasicParsing -Uri "https://api.nuget.org/v3-flatcontainer/python/$PythonVersion/python.$PythonVersion.nupkg" -OutFile $package
    [IO.Compression.ZipFile]::ExtractToDirectory($package, (Join-Path $temp 'python'))
    Move-Item -LiteralPath (Join-Path $temp 'python/tools') -Destination (Join-Path $bundle 'runtime')
    $python = Join-Path $bundle 'runtime/python.exe'
    & $python -I -m pip install --disable-pip-version-check --no-cache-dir 'tzdata>=2024.1'
    if ($LASTEXITCODE -ne 0) { throw 'Could not install the worker runtime dependency.' }

    $source = Join-Path $repo 'alisdr'
    Get-ChildItem -LiteralPath $source -Recurse -File | Where-Object {
        $_.Extension -ne '.pyc' -and $_.DirectoryName -notmatch '[\\/]__pycache__(?:[\\/]|$)'
    } | ForEach-Object {
        $target = Join-Path $bundle ('alisdr/' + $_.FullName.Substring($source.Length + 1))
        [IO.Directory]::CreateDirectory([IO.Path]::GetDirectoryName($target)) | Out-Null
        [IO.File]::Copy($_.FullName, $target)
    }
    foreach ($name in @('launch.py', 'Start AliSDR Worker.cmd', 'Start Here.txt')) {
        Copy-Item -LiteralPath (Join-Path $PSScriptRoot $name) -Destination $bundle
    }
    Copy-Item -LiteralPath (Join-Path $repo 'LICENSE') -Destination $bundle
    Push-Location $bundle
    try {
        & $python -B -E -s -m alisdr.worker --help
        if ($LASTEXITCODE -ne 0) { throw 'The bundled worker failed its startup check.' }
    } finally {
        Pop-Location
    }
    [IO.Directory]::CreateDirectory([IO.Path]::GetDirectoryName($output)) | Out-Null
    [IO.Compression.ZipFile]::CreateFromDirectory($bundle, $output)
    Write-Output "Built $output"
} finally {
    $resolved = (Resolve-Path -LiteralPath $temp).Path
    if ($resolved -eq $temp -and [IO.Path]::GetDirectoryName($resolved) -eq $tempRoot -and
        -not ((Get-Item -LiteralPath $resolved).Attributes -band [IO.FileAttributes]::ReparsePoint)) {
        Remove-Item -LiteralPath $resolved -Recurse -Force
    }
}

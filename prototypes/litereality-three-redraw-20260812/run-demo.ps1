param(
    [string]$DataPath = '',
    [string]$OutputPath = '',
    [int]$Port = 8765,
    [string]$PythonPath = 'python',
    [switch]$GenerateOnly
)

$ErrorActionPreference = 'Stop'
$repoRoot = (Resolve-Path -LiteralPath (Join-Path $PSScriptRoot '..\..')).Path
$generatorPath = Join-Path $PSScriptRoot 'generate_demo.py'
$endpointMeasurePath = Join-Path $PSScriptRoot 'measure_structure_endpoints.py'
if ([string]::IsNullOrWhiteSpace($OutputPath)) {
    $OutputPath = Join-Path $PSScriptRoot 'generated'
}

if (-not [string]::IsNullOrWhiteSpace($DataPath)) {
    if (-not (Test-Path -LiteralPath $DataPath -PathType Container)) {
        throw "Dataset directory was not found: $DataPath"
    }
    & $PythonPath $generatorPath --data $DataPath --output $OutputPath
    if ($LASTEXITCODE -ne 0) {
        throw "Demo generation failed with exit code $LASTEXITCODE"
    }
    & $PythonPath $endpointMeasurePath `
        --manual (Join-Path $PSScriptRoot 'manual-review.json') `
        --image (Join-Path $OutputPath 'focus-pointcloud-high-structure-slice.png') `
        --metadata (Join-Path $OutputPath 'focus-orthophoto-metadata.json') `
        --output (Join-Path $OutputPath 'structure-endpoint-suggestions.json')
    if ($LASTEXITCODE -ne 0) {
        throw "Endpoint suggestion measurement failed with exit code $LASTEXITCODE"
    }
} elseif (-not (Test-Path -LiteralPath (Join-Path $OutputPath 'scene.json') -PathType Leaf)) {
    throw "No portable scene exists. Pass -DataPath to generate one from a local dataset."
}

if (-not $GenerateOnly) {
    Write-Host "Open http://127.0.0.1:$Port/prototypes/litereality-three-redraw-20260812/viewer.html"
    & $PythonPath -m http.server $Port --bind 127.0.0.1 --directory $repoRoot
}

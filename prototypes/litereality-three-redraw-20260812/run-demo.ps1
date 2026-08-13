param(
    [string]$DataPath = '',
    [string]$OutputPath = '',
    [int]$Port = 8765,
    [string]$PythonPath = 'python',
    [switch]$GenerateOnly,
    [switch]$Sample
)

$ErrorActionPreference = 'Stop'
$repoRoot = (Resolve-Path -LiteralPath (Join-Path $PSScriptRoot '..\..')).Path
$generatorPath = Join-Path $PSScriptRoot 'generate_demo.py'
$endpointMeasurePath = Join-Path $PSScriptRoot 'measure_structure_endpoints.py'
$sampleGeneratorPath = Join-Path $repoRoot 'scene-core\make_sample_scene.py'
if ([string]::IsNullOrWhiteSpace($OutputPath)) {
    $OutputPath = Join-Path $PSScriptRoot 'generated'
}

if ($Sample) {
    # Fully synthetic Scene V2 sample: works on a fresh clone with no capture.
    & $PythonPath $sampleGeneratorPath --output $OutputPath
    if ($LASTEXITCODE -ne 0) {
        throw "Sample scene generation failed with exit code $LASTEXITCODE"
    }
} elseif (-not [string]::IsNullOrWhiteSpace($DataPath)) {
    if (-not (Test-Path -LiteralPath $DataPath -PathType Container)) {
        throw "Dataset directory was not found: $DataPath"
    }
    # Capture-specific generators are authored per dataset by the
    # reconstruct-indoor-scene skill and are intentionally not part of the
    # portable repository. Fail closed with a pointer instead of a stack trace.
    if (-not (Test-Path -LiteralPath $generatorPath -PathType Leaf)) {
        throw ("generate_demo.py does not exist in this checkout. Author it for your capture via " +
            ".codex/skills/reconstruct-indoor-scene (init_reconstruction_job.py), or run with -Sample " +
            "to view the synthetic Scene V2 demo.")
    }
    & $PythonPath $generatorPath --data $DataPath --output $OutputPath
    if ($LASTEXITCODE -ne 0) {
        throw "Demo generation failed with exit code $LASTEXITCODE"
    }
    if (Test-Path -LiteralPath $endpointMeasurePath -PathType Leaf) {
        & $PythonPath $endpointMeasurePath `
            --manual (Join-Path $PSScriptRoot 'manual-review.json') `
            --image (Join-Path $OutputPath 'focus-pointcloud-high-structure-slice.png') `
            --metadata (Join-Path $OutputPath 'focus-orthophoto-metadata.json') `
            --output (Join-Path $OutputPath 'structure-endpoint-suggestions.json')
        if ($LASTEXITCODE -ne 0) {
            throw "Endpoint suggestion measurement failed with exit code $LASTEXITCODE"
        }
    } else {
        Write-Warning 'measure_structure_endpoints.py is not present; skipping endpoint suggestions.'
    }
} elseif (-not (Test-Path -LiteralPath (Join-Path $OutputPath 'scene.json') -PathType Leaf)) {
    throw 'No portable scene exists. Pass -DataPath to generate one from a local dataset, or -Sample for the synthetic Scene V2 demo.'
}

if (-not $GenerateOnly) {
    Write-Host "Open http://127.0.0.1:$Port/prototypes/litereality-three-redraw-20260812/viewer.html"
    & $PythonPath -m http.server $Port --bind 127.0.0.1 --directory $repoRoot
}

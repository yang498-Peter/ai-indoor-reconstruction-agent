param(
  [Parameter(Mandatory = $true)]
  [string]$Source,

  [Parameter(Mandatory = $true)]
  [string]$Destination,

  [ValidateRange(250, 60000)]
  [int]$IntervalMilliseconds = 1000
)

$ErrorActionPreference = 'Stop'
$sourcePath = [System.IO.Path]::GetFullPath($Source)
$destinationPath = [System.IO.Path]::GetFullPath($Destination)
$destinationDirectory = [System.IO.Path]::GetDirectoryName($destinationPath)
$temporaryPath = "$destinationPath.sync-tmp"

if (-not (Test-Path -LiteralPath $sourcePath -PathType Leaf)) {
  throw "Scene presentation source does not exist: $sourcePath"
}

[System.IO.Directory]::CreateDirectory($destinationDirectory) | Out-Null
$lastHash = $null

while ($true) {
  $currentHash = (Get-FileHash -LiteralPath $sourcePath -Algorithm SHA256).Hash
  if ($currentHash -ne $lastHash) {
    $payload = [System.IO.File]::ReadAllBytes($sourcePath)
    [System.IO.File]::WriteAllBytes($temporaryPath, $payload)
    Move-Item -LiteralPath $temporaryPath -Destination $destinationPath -Force
    $lastHash = $currentHash
  }

  Start-Sleep -Milliseconds $IntervalMilliseconds
}

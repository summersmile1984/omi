param(
    [Parameter(Mandatory = $true)]
    [string]$Artifact
)

$ErrorActionPreference = "Stop"
if (-not (Test-Path -LiteralPath $Artifact -PathType Leaf)) {
    throw "self-host artifact not found: $Artifact"
}

Add-Type -AssemblyName System.IO.Compression.FileSystem
$scanRoot = Join-Path ([IO.Path]::GetTempPath()) ("omi-selfhost-android-" + [Guid]::NewGuid().ToString("N"))
[IO.Directory]::CreateDirectory($scanRoot) | Out-Null
$archive = $null
try {
    $archive = [IO.Compression.ZipFile]::OpenRead((Resolve-Path $Artifact).Path)
    $forbiddenNames = @("google-services.json", "GoogleService-Info.plist", "google_app_id.xml")
    $officialOmiOriginPattern = 'https?://([^/@\s]+\.)?omi\.me([/:?#]|$)'
    $forbiddenPattern = 'AIza[0-9A-Za-z_-]{30,}|phc_[0-9A-Za-z_-]{12,}|[0-9]+-[0-9A-Za-z_-]+\.apps\.googleusercontent\.com|[a-z0-9-]+\.firebaseapp\.com|[a-z0-9-]+\.firebaseio\.com'
    $entryIndex = 0
    foreach ($entry in $archive.Entries) {
        if ([string]::IsNullOrEmpty($entry.Name)) {
            continue
        }
        if ($forbiddenNames -contains $entry.Name) {
            throw "self-host Android artifact contains managed Firebase configuration"
        }
        # APKs can contain distinct case-sensitive names that collide on Windows.
        # Copy each entry to a unique scan path so neither payload is overwritten.
        $scanFile = Join-Path $scanRoot ("entry-{0:D8}.bin" -f $entryIndex)
        $entryIndex += 1
        $inputStream = $entry.Open()
        $outputStream = [IO.File]::Create($scanFile)
        try {
            $inputStream.CopyTo($outputStream)
        }
        finally {
            $outputStream.Dispose()
            $inputStream.Dispose()
        }
        $ascii = [Text.Encoding]::ASCII.GetString([IO.File]::ReadAllBytes($scanFile))
        if ($ascii -match $officialOmiOriginPattern) {
            throw "self-host Android artifact contains an official Omi-managed origin"
        }
        if ($ascii -match $forbiddenPattern) {
            throw "self-host Android artifact contains populated managed-client credentials/origins"
        }
        Remove-Item -LiteralPath $scanFile -Force
    }
}
finally {
    if ($null -ne $archive) {
        $archive.Dispose()
    }
    Remove-Item -LiteralPath $scanRoot -Recurse -Force -ErrorAction SilentlyContinue
}

Write-Output "self-host Android artifact contains no packaged managed-client configuration or credentials"

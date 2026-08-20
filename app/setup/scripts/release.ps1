$ErrorActionPreference = "Stop"

function Invoke-CheckedNative {
    param(
        [Parameter(Mandatory = $true)][string]$Command,
        [Parameter(Mandatory = $true)][string[]]$Arguments
    )
    & $Command @Arguments
    if ($LASTEXITCODE -ne 0) {
        throw "$Command failed with exit code $LASTEXITCODE"
    }
}

$appProfile = if ($env:OMI_APP_PROFILE) { $env:OMI_APP_PROFILE } elseif ($env:OMI_AUTH_PROVIDER -eq "better_auth") { "self_hosted" } else { "mobile_beta" }
if ($env:OMI_AUTH_PROVIDER -eq "better_auth" -and $appProfile -ne "self_hosted") {
    throw "OMI_AUTH_PROVIDER=better_auth requires OMI_APP_PROFILE=self_hosted for this release lane"
}
if ($appProfile -eq "self_hosted" -and $env:OMI_AUTH_PROVIDER -ne "better_auth") {
    throw "OMI_APP_PROFILE=self_hosted requires OMI_AUTH_PROVIDER=better_auth for this release lane"
}
$androidFlavor = if ($appProfile -eq "self_hosted") { "selfhost" } else { "prod" }
$flutterArgs = @(
    "--release",
    "--flavor", $androidFlavor,
    "-t", "lib/main.dart",
    "--dart-define=OMI_APP_PROFILE=$appProfile"
)

if ($env:OMI_API_BASE_URL) {
    $flutterArgs += "--dart-define=OMI_API_BASE_URL=$($env:OMI_API_BASE_URL)"
}
if ($env:OMI_AUTH_PROVIDER) {
    if ($env:OMI_AUTH_PROVIDER -eq "better_auth" -and -not $env:OMI_AUTH_SERVER_URL) {
        throw "OMI_AUTH_SERVER_URL is required when OMI_AUTH_PROVIDER=better_auth"
    }
    if ($env:OMI_AUTH_PROVIDER -eq "better_auth" -and -not $env:OMI_API_BASE_URL) {
        throw "OMI_API_BASE_URL is required when OMI_AUTH_PROVIDER=better_auth"
    }
    if ($env:OMI_AUTH_PROVIDER -eq "better_auth") {
        foreach ($publicOrigin in @("OMI_PRIVACY_URL", "OMI_TERMS_URL", "OMI_SHARE_BASE_URL", "OMI_MCP_BASE_URL")) {
            $publicOriginValue = [Environment]::GetEnvironmentVariable($publicOrigin)
            if (-not $publicOriginValue) {
                throw "$publicOrigin is required when OMI_AUTH_PROVIDER=better_auth"
            }
            $flutterArgs += "--dart-define=$publicOrigin=$publicOriginValue"
        }
    }
    $flutterArgs += "--dart-define=OMI_AUTH_PROVIDER=$($env:OMI_AUTH_PROVIDER)"
    if ($env:OMI_AUTH_PROVIDER -eq "better_auth") {
        $env:OMI_FIREBASE_SERVICES_ENABLED = "false"
        $flutterArgs += "--dart-define=OMI_FIREBASE_SERVICES_ENABLED=false"
    }
    if ($env:OMI_AUTH_SERVER_URL) {
        $flutterArgs += "--dart-define=OMI_AUTH_SERVER_URL=$($env:OMI_AUTH_SERVER_URL)"
    }
}

Invoke-CheckedNative "flutter" @("clean")
if ($appProfile -eq "self_hosted") {
    $envFile = Join-Path (Get-Location) ".env"
    $generatedFile = Join-Path (Get-Location) "lib/env/prod_env.g.dart"
    $analysisFile = Join-Path (Get-Location) "analysis_options.yaml"
    $lockFile = Join-Path (Get-Location) "pubspec.lock"
    $envBackup = [IO.Path]::GetTempFileName()
    $generatedBackup = [IO.Path]::GetTempFileName()
    $analysisBackup = [IO.Path]::GetTempFileName()
    $lockBackup = [IO.Path]::GetTempFileName()
    $hadEnv = Test-Path $envFile
    $hadGenerated = Test-Path $generatedFile
    $hadAnalysis = Test-Path $analysisFile
    $hadLock = Test-Path $lockFile
    if ($hadEnv) { Copy-Item $envFile $envBackup -Force }
    if ($hadGenerated) { Copy-Item $generatedFile $generatedBackup -Force }
    if ($hadAnalysis) { Copy-Item $analysisFile $analysisBackup -Force }
    if ($hadLock) { Copy-Item $lockFile $lockBackup -Force }
    $buildError = $null
    $lockChanged = $false
    try {
        @(
            "API_BASE_URL=$($env:OMI_API_BASE_URL)",
            "USE_WEB_AUTH=false",
            "USE_AUTH_CUSTOM_TOKEN=false"
        ) | Set-Content -Path $envFile -Encoding utf8
        Invoke-CheckedNative "flutter" @("pub", "get", "--enforce-lockfile")
        Invoke-CheckedNative "dart" @("run", "build_runner", "build")
        $generated = Get-Content $generatedFile -Raw
        foreach ($field in @("posthogApiKey", "googleMapsApiKey", "intercomAppId", "intercomIOSApiKey", "intercomAndroidApiKey", "googleClientId", "googleClientSecret")) {
            if ($generated -notmatch "static final String\? $field = null;") {
                throw "self-host codegen embedded managed client value: $field"
            }
        }
        Invoke-CheckedNative "flutter" (@("build", "appbundle") + $flutterArgs)
        Invoke-CheckedNative "flutter" (@("build", "apk") + $flutterArgs)
        & (Join-Path (Get-Location) "scripts/smoke_android_self_host_artifact.ps1") `
            (Join-Path (Get-Location) "build/app/outputs/bundle/selfhostRelease/app-selfhost-release.aab")
        & (Join-Path (Get-Location) "scripts/smoke_android_self_host_artifact.ps1") `
            (Join-Path (Get-Location) "build/app/outputs/flutter-apk/app-selfhost-release.apk")
    }
    catch {
        $buildError = $_
    }
    finally {
        if ($hadLock) {
            $lockChanged = (-not (Test-Path -LiteralPath $lockFile -PathType Leaf)) -or (
                [Convert]::ToBase64String([IO.File]::ReadAllBytes($lockFile)) -ne
                [Convert]::ToBase64String([IO.File]::ReadAllBytes($lockBackup))
            )
        }
        elseif (Test-Path -LiteralPath $lockFile) {
            $lockChanged = $true
        }
        if ($hadEnv) { Copy-Item $envBackup $envFile -Force } else { Remove-Item $envFile -Force -ErrorAction SilentlyContinue }
        if ($hadGenerated) { Copy-Item $generatedBackup $generatedFile -Force } else { Remove-Item $generatedFile -Force -ErrorAction SilentlyContinue }
        if ($hadAnalysis) { Copy-Item $analysisBackup $analysisFile -Force } else { Remove-Item $analysisFile -Force -ErrorAction SilentlyContinue }
        if ($hadLock) { Copy-Item $lockBackup $lockFile -Force } else { Remove-Item $lockFile -Force -ErrorAction SilentlyContinue }
        Remove-Item $envBackup, $generatedBackup, $analysisBackup, $lockBackup -Force -ErrorAction SilentlyContinue
    }
    if ($lockChanged) {
        throw "self-host build changed pubspec.lock; use the repository-pinned Flutter SDK and --enforce-lockfile"
    }
    if ($null -ne $buildError) {
        throw $buildError
    }
}
else {
    Invoke-CheckedNative "flutter" @("pub", "get")
    Invoke-CheckedNative "dart" @("run", "build_runner", "build")
    Invoke-CheckedNative "flutter" (@("build", "appbundle") + $flutterArgs)
    Invoke-CheckedNative "flutter" (@("build", "apk") + $flutterArgs)
}

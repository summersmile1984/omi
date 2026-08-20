$ErrorActionPreference = "Stop"

$appProfile = if ($env:OMI_APP_PROFILE) { $env:OMI_APP_PROFILE } elseif ($env:OMI_AUTH_PROVIDER -eq "better_auth") { "self_hosted" } else { "mobile_beta" }
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
        foreach ($publicOrigin in @("OMI_PRIVACY_URL", "OMI_TERMS_URL", "OMI_SHARE_BASE_URL")) {
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

flutter clean
flutter pub get
dart run build_runner build
flutter build appbundle @flutterArgs
flutter build apk @flutterArgs

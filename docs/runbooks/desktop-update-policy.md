# Desktop Update Policy

Use this when macOS desktop users need an extra update prompt beyond Sparkle.

The managed profile keeps the historical `api.omi.me` recovery URL when no
policy document is available. Neutral/self-hosted deployments never use that
default: set `DESKTOP_UPDATE_DOWNLOAD_URL` to an operator-owned HTTPS repair or
installer page, or put an explicit operator-owned `download_url` in the
Firestore policy document. Until one is present, the API returns typed
`availability=disabled` with `download_url=null` and no update prompt.

## Firestore Config

Create or update `desktop_update_policy/current`:

```json
{
  "active": true,
  "severity": "required",
  "maximum_build_number": 11507,
  "latest_build_number": 11590,
  "title": "Update required",
  "message": "Your Omi desktop app has an older updater issue. Please install the latest version manually.",
  "cta_text": "Download latest",
  "download_url": "https://storage.googleapis.com/omi_macos_updates/stable/index.html",
  "can_dismiss": false,
  "platforms": ["macos"]
}
```

## Fields

- `active`: `true` enables the policy.
- `severity`: `banner` shows a dismissible top banner; `required` shows a blocking prompt; `none` disables it.
- `maximum_build_number`: highest client build that should see the policy. Clients above this build do not see it.
- `latest_build_number`: informational for clients and analytics.
- `title`, `message`, `cta_text`: user-facing copy.
- `download_url`: manual installer URL. For legacy recovery, use the static
  stable repair page published by the stable-promotion workflow instead of the
  dynamic appcast/download API. In a neutral deployment this must be an
  operator-owned URL; `api.omi.me` and other Omi-operated hosts are rejected.
- `can_dismiss`: only applies to `banner`; required prompts cannot be dismissed.
- `platforms`: optional allowlist. Omit or use `["macos"]` for macOS.

## Verification

```bash
curl 'https://api.omi.me/v2/desktop/update-policy?platform=macos&current_build=11400'
curl 'https://api.omi.me/v2/desktop/appcast.xml?platform=macos' | grep criticalUpdate
curl -fsS 'https://storage.googleapis.com/omi_macos_updates/stable/latest.json' | python3 -m json.tool
```

Disable the policy by setting `active` to `false`.

For a self-hosted deployment, verify the fail-closed default and the explicit
operator path:

```bash
OMI_DEPLOYMENT_PROFILE=self_hosted \
  curl -fsS 'https://api.example.com/v2/desktop/update-policy?platform=macos' \
  | python3 -m json.tool

DESKTOP_UPDATE_DOWNLOAD_URL=https://objects.example.com/desktop/stable.html
```

The first response must contain `availability: "disabled"` and a null
`download_url` until the environment variable or Firestore manifest is
configured. The checked-in Compose profile binds the optional environment
variable without making it required.

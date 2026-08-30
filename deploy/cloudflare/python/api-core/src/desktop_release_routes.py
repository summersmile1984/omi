"""Public desktop update feeds backed by the Cloudflare D1 projection.

The release pipeline owns artifact publication and promotion. This Worker only
serves signed metadata that has been explicitly projected into D1; an empty
projection therefore fails closed for downloads and latest-version lookups.
"""

from __future__ import annotations

import html
import hashlib
import json
import re
from datetime import datetime
from urllib.parse import urlparse

from fastapi import APIRouter, HTTPException, Query, Request
from fastapi.responses import HTMLResponse, JSONResponse, RedirectResponse, Response

from fallback import record_fallback

router = APIRouter()

DEFAULT_DOWNLOAD_URL = "https://api.omi.me/v2/desktop/download/latest?channel=stable"
VALID_PLATFORMS = {"macos", "windows", "linux"}
VALID_SEVERITIES = {"none", "banner", "required"}
PREVIEW_BUCKET_HOST = "storage.googleapis.com"
PREVIEW_BUCKET_NAME = "omi_macos_updates"
PREVIEW_SLUG_RE = re.compile(r"^[a-z0-9](?:[a-z0-9-]{0,61}[a-z0-9])?$")
PREVIEW_SHA40_RE = re.compile(r"^[0-9a-f]{40}$")
PREVIEW_SHA256_RE = re.compile(r"^[0-9a-f]{64}$")


def _https_url(value: object, default: str | None = None) -> str | None:
    candidate = value.strip() if isinstance(value, str) else ""
    parsed = urlparse(candidate)
    if parsed.scheme.lower() not in {"http", "https"} or not parsed.netloc:
        return default
    return candidate


def _preview_slug(value: object) -> str | None:
    if not isinstance(value, str):
        return None
    candidate = value.strip()
    return candidate if PREVIEW_SLUG_RE.fullmatch(candidate) else None


def _preview_source_sha(value: object) -> str | None:
    if not isinstance(value, str):
        return None
    candidate = value.strip().lower()
    return candidate if PREVIEW_SHA40_RE.fullmatch(candidate) else None


def _preview_identity(slug: str) -> str:
    return f"p{hashlib.sha256(slug.encode('utf-8')).hexdigest()[:10]}"


def _preview_manifest(row: object, *, slug: str, source_sha: str) -> dict[str, object] | None:
    if not isinstance(row, dict):
        return None
    if _preview_slug(row.get("slug")) != slug or _preview_source_sha(row.get("source_sha")) != source_sha:
        return None
    app_name = row.get("app_name")
    bundle_id = row.get("bundle_id")
    url_scheme = row.get("url_scheme")
    built_at = row.get("built_at")
    signer = row.get("signer")
    notarization = row.get("notarization")
    dmg_sha256 = row.get("dmg_sha256")
    if (
        not isinstance(app_name, str)
        or not app_name.startswith("Omi Preview")
        or not isinstance(bundle_id, str)
        or bundle_id != f"com.omi.preview.{_preview_identity(slug)}"
        or not isinstance(url_scheme, str)
        or url_scheme != f"omi-preview-{_preview_identity(slug)}"
        or not isinstance(built_at, str)
        or not built_at.strip()
        or not isinstance(signer, str)
        or not signer.strip()
        or notarization != "stapled"
        or not isinstance(dmg_sha256, str)
        or not PREVIEW_SHA256_RE.fullmatch(dmg_sha256.lower())
    ):
        return None
    try:
        datetime.fromisoformat(built_at.replace("Z", "+00:00"))
    except (TypeError, ValueError):
        return None
    dmg_url = row.get("dmg_url")
    parsed = urlparse(dmg_url) if isinstance(dmg_url, str) else None
    expected_path = f"/{PREVIEW_BUCKET_NAME}/previews/{slug}/{source_sha}/Omi-Preview.dmg"
    if (
        parsed is None
        or parsed.scheme != "https"
        or parsed.netloc != PREVIEW_BUCKET_HOST
        or parsed.path != expected_path
        or parsed.params
        or parsed.query
        or parsed.fragment
    ):
        return None
    notes = row.get("notes")
    if notes is not None and not isinstance(notes, str):
        return None
    backend_url = row.get("backend_url")
    if backend_url is not None:
        backend_parsed = urlparse(backend_url) if isinstance(backend_url, str) else None
        if (
            backend_parsed is None
            or backend_parsed.scheme != "https"
            or not backend_parsed.netloc
            or backend_parsed.params
            or backend_parsed.query
            or backend_parsed.fragment
        ):
            return None
    return {
        "slug": slug,
        "source_sha": source_sha,
        "dmg_url": dmg_url,
        "dmg_sha256": dmg_sha256.lower(),
        "app_name": app_name,
        "bundle_id": bundle_id,
        "url_scheme": url_scheme,
        "built_at": built_at,
        "signer": signer,
        "notarization": notarization,
        "notes": notes,
        "backend_url": backend_url,
    }


def _manual_download_url(release: dict[str, object]) -> str:
    explicit = release.get("manual_download_url")
    if isinstance(explicit, str) and explicit:
        return explicit
    download_url = str(release["download_url"])
    if download_url.endswith("/Omi.zip"):
        return f"{download_url[:-len('Omi.zip')]}Omi.dmg"
    return download_url


def _download_landing_html(url: str, *, platform: str, channel: str, version: str, notice: str = "") -> str:
    escaped_url = html.escape(url, quote=True)
    escaped_version = html.escape(version)
    escaped_channel = "Beta " if channel == "beta" else ""
    escaped_notice = f'<p class="notice">{html.escape(notice)}</p>' if notice else ""
    os_name = "Windows" if platform == "windows" else "macOS"
    return f"""<!DOCTYPE html>
<html lang="en">
<head>
  <meta charset="utf-8">
  <meta name="viewport" content="width=device-width, initial-scale=1">
  <meta http-equiv="refresh" content="2;url={escaped_url}">
  <title>Download Omi {escaped_channel}for {os_name}</title>
  <style>body{{margin:0;min-height:100vh;display:grid;place-items:center;background:#0a0a0a;color:#f5f5f5;font-family:-apple-system,BlinkMacSystemFont,"Segoe UI",sans-serif;text-align:center}}main{{max-width:620px;padding:40px}}a{{color:#fff}}.notice{{color:#fbbf24}}.version{{color:#999}}</style>
</head>
<body><main>
  <h1>Downloading Omi {escaped_channel}for {os_name}</h1>
  <p class="version">v{escaped_version}</p>
  {escaped_notice}
  <p>Your download should start automatically.</p>
  <p><a href="{escaped_url}">Click here if the download does not start</a></p>
</main></body>
</html>"""


def _bool(value: object, default: bool = False) -> bool:
    if isinstance(value, bool):
        return value
    if isinstance(value, int) and value in (0, 1):
        return bool(value)
    return default


def _int(value: object) -> int | None:
    if isinstance(value, bool):
        return None
    if isinstance(value, int) and value >= 0:
        return value
    if isinstance(value, str):
        try:
            parsed = int(value)
        except ValueError:
            return None
        return parsed if parsed >= 0 else None
    return None


def _changelog(value: object) -> list[str]:
    if not isinstance(value, str):
        return []
    try:
        decoded = json.loads(value)
    except (TypeError, ValueError):
        return []
    if not isinstance(decoded, list):
        return []
    return [item.strip() for item in decoded if isinstance(item, str) and item.strip()][:100]


def _release(row: dict[str, object]) -> dict[str, object] | None:
    version = row.get("version")
    download_url = _https_url(row.get("download_url"))
    signature = row.get("ed_signature")
    published_at = row.get("published_at")
    channel = row.get("channel")
    build_number = _int(row.get("build_number"))
    if not all(isinstance(value, str) and value for value in (version, signature, published_at)):
        return None
    if download_url is None or build_number is None or channel not in {"staging", "beta", "stable"}:
        return None
    return {
        "version": version,
        "build_number": build_number,
        "download_url": download_url,
        "manual_download_url": _https_url(row.get("manual_download_url")),
        "ed_signature": signature,
        "published_at": published_at,
        "changelog": _changelog(row.get("changelog_json")),
        "is_live": _bool(row.get("is_live")),
        "is_critical": _bool(row.get("is_critical")),
        "channel": channel,
        "windows_feed_url": _https_url(row.get("windows_feed_url")),
    }


async def _live_releases(request: Request) -> list[dict[str, object]]:
    rows = (
        await request.scope["env"]
        .APP_DB.prepare(
            "SELECT version, build_number, download_url, manual_download_url, ed_signature, published_at, "
            "changelog_json, is_live, is_critical, channel, windows_feed_url "
            "FROM cf_desktop_releases WHERE is_live = 1 ORDER BY build_number DESC, id DESC LIMIT 100"
        )
        .all()
    )
    raw_rows = rows.get("results", []) if isinstance(rows, dict) else []
    return [release for row in raw_rows if isinstance(row, dict) and (release := _release(row)) is not None]


def _default_policy() -> dict[str, object]:
    return {
        "id": "current",
        "active": False,
        "severity": "none",
        "maximum_build_number": None,
        "latest_build_number": None,
        "title": None,
        "message": None,
        "cta_text": "Download latest",
        "download_url": DEFAULT_DOWNLOAD_URL,
        "can_dismiss": True,
    }


def _policy(row: dict[str, object] | None, *, platform: str, current_build: int | None) -> dict[str, object]:
    if not row:
        return _default_policy()
    severity = row.get("severity") if row.get("severity") in VALID_SEVERITIES else "none"
    platforms: list[str] = []
    raw_platforms = row.get("platforms_json")
    if isinstance(raw_platforms, str):
        try:
            parsed = json.loads(raw_platforms)
        except (TypeError, ValueError):
            parsed = []
        if isinstance(parsed, list):
            platforms = [item.strip() for item in parsed if isinstance(item, str) and item.strip()][:10]
    maximum = _int(row.get("maximum_build_number"))
    if platforms and platform not in platforms:
        return _default_policy()
    if current_build is not None and maximum is not None and current_build > maximum:
        return _default_policy()
    if not _bool(row.get("active")):
        return _default_policy()
    return {
        "id": row.get("id") if isinstance(row.get("id"), str) and row.get("id") else "current",
        "active": True,
        "severity": severity,
        "maximum_build_number": maximum,
        "latest_build_number": _int(row.get("latest_build_number")),
        "title": row.get("title") if isinstance(row.get("title"), str) and row.get("title").strip() else None,
        "message": row.get("message") if isinstance(row.get("message"), str) and row.get("message").strip() else None,
        "cta_text": (
            row.get("cta_text")
            if isinstance(row.get("cta_text"), str) and row.get("cta_text").strip()
            else "Download latest"
        ),
        "download_url": _https_url(row.get("download_url"), DEFAULT_DOWNLOAD_URL),
        "can_dismiss": _bool(row.get("can_dismiss"), default=True),
        "platforms": platforms,
    }


def _appcast_xml(releases: list[dict[str, object]], platform: str) -> str:
    lines = [
        '<?xml version="1.0" encoding="utf-8"?>',
        '<rss version="2.0" xmlns:sparkle="http://www.andymatuschak.org/xml-namespaces/sparkle">',
        "  <channel>",
        "    <title>Omi Desktop Updates</title>",
        "    <description>Omi AI Desktop Application</description>",
        "    <language>en</language>",
    ]
    seen_channels: set[str] = set()
    for release in releases:
        channel = str(release["channel"])
        if channel in seen_channels:
            continue
        seen_channels.add(channel)
        changes = "<p>Bug fixes and improvements.</p>"
        changelog = release["changelog"]
        if isinstance(changelog, list) and changelog:
            changes = "<ul>" + "".join(f"<li>{html.escape(str(item))}</li>" for item in changelog) + "</ul>"
        safe_cdata = changes.replace("]]>", "]] ]]><![CDATA[>".replace(" ", ""))
        lines.extend(
            [
                "    <item>",
                f"      <title>Omi {html.escape(str(release['version']))}</title>",
                f"      <sparkle:version>{release['build_number']}</sparkle:version>",
                f"      <sparkle:shortVersionString>{html.escape(str(release['version']))}</sparkle:shortVersionString>",
                f"      <description><![CDATA[{safe_cdata}]]></description>",
                f"      <pubDate>{html.escape(str(release['published_at']))}</pubDate>",
                f'      <enclosure url="{html.escape(str(release["download_url"]), quote=True)}" type="application/octet-stream" sparkle:os="{html.escape(platform, quote=True)}" sparkle:edSignature="{html.escape(str(release["ed_signature"]), quote=True)}" />',
            ]
        )
        if channel != "stable":
            lines.append(f"      <sparkle:channel>{html.escape(channel)}</sparkle:channel>")
        if release["is_critical"]:
            lines.append("      <sparkle:criticalUpdate />")
        lines.append("    </item>")
    lines.extend(["  </channel>", "</rss>", ""])
    return "\n".join(lines)


@router.get("/appcast.xml")
async def get_appcast(request: Request, platform: str = Query(default="macos")):
    if platform not in VALID_PLATFORMS:
        raise HTTPException(status_code=422, detail="Invalid platform")
    try:
        releases = await _live_releases(request)
    except Exception:
        return Response("Desktop update feed is temporarily unavailable", status_code=503, media_type="text/plain")
    return Response(
        _appcast_xml(releases, platform), media_type="application/xml", headers={"Cache-Control": "max-age=300"}
    )


async def _stable_release(request: Request) -> dict[str, object]:
    releases = await _live_releases(request)
    for release in releases:
        if release["channel"] == "stable":
            return release
    raise HTTPException(status_code=404, detail="No live releases found")


def _platform(value: str) -> str:
    if value not in VALID_PLATFORMS:
        raise HTTPException(status_code=422, detail="Invalid platform")
    return value


def _channel(value: str) -> str:
    if value not in {"stable", "beta"}:
        raise HTTPException(status_code=422, detail="Invalid channel")
    return value


def _pick_channel(releases: list[dict[str, object]], channel: str) -> dict[str, object] | None:
    return next((release for release in releases if release["channel"] == channel), None)


@router.get("/updates/latest")
async def get_latest_version(request: Request):
    try:
        release = await _stable_release(request)
    except HTTPException:
        raise
    except Exception as exc:
        raise HTTPException(status_code=500, detail="Failed to fetch releases") from exc
    return {key: release[key] for key in ("version", "build_number", "download_url", "is_critical")}


@router.get("/download")
async def download_redirect(request: Request):
    try:
        release = await _stable_release(request)
    except HTTPException:
        raise
    except Exception as exc:
        raise HTTPException(status_code=500, detail="Failed to fetch releases") from exc
    return RedirectResponse(_manual_download_url(release), status_code=307)


@router.get("/v2/desktop/appcast.xml")
async def get_desktop_appcast(
    request: Request,
    platform: str = Query(default="macos"),
    identity: str = Query(default="stable"),
):
    platform = _platform(platform)
    if identity not in {"stable", "beta"}:
        raise HTTPException(status_code=422, detail="Invalid identity")
    try:
        releases = await _live_releases(request)
    except Exception as exc:
        raise HTTPException(status_code=500, detail="Error generating appcast") from exc
    if not releases:
        raise HTTPException(status_code=404, detail=f"No desktop releases found for platform: {platform}")
    wanted = "beta" if identity == "beta" else "stable"
    return Response(
        _appcast_xml([release for release in releases if release["channel"] == wanted], platform),
        media_type="application/xml",
        headers={"Cache-Control": "max-age=300"},
    )


@router.get("/v2/desktop/download/latest")
async def download_latest_desktop(
    request: Request,
    platform: str = "macos",
    channel: str = "stable",
    identity: str | None = None,
):
    platform = _platform(platform)
    channel = _channel(channel)
    if identity is not None and identity not in {"stable", "beta"}:
        raise HTTPException(status_code=422, detail="Invalid identity")
    if identity == "beta":
        channel = "beta"
    effective_identity = identity or channel
    try:
        releases = await _live_releases(request)
    except Exception as exc:
        raise HTTPException(status_code=500, detail="Failed to fetch releases") from exc
    if not releases:
        raise HTTPException(status_code=404, detail=f"No live desktop releases found for platform: {platform}")
    release = _pick_channel(releases, channel)
    if release is None:
        raise HTTPException(status_code=404, detail=f"No installer found for platform {platform}, channel: {channel}")
    url = _manual_download_url(release)
    return HTMLResponse(
        _download_landing_html(
            url,
            platform=platform,
            channel="beta" if effective_identity == "beta" else "stable",
            version=str(release["version"]),
        )
    )


@router.get("/v2/desktop/download/beta")
async def download_beta_desktop(request: Request, platform: str = Query(default="macos")):
    return await download_latest_desktop(request, platform=platform, channel="beta", identity="beta")


_PREVIEW_SELECT = (
    "SELECT slug, source_sha, dmg_url, dmg_sha256, app_name, bundle_id, url_scheme, built_at, signer, "
    "notarization, notes, backend_url FROM cf_desktop_preview_manifests "
)


async def _preview_from_d1(request: Request, slug: str, source_sha: str | None = None) -> dict[str, object] | None:
    env = request.scope["env"]
    resolved_sha = source_sha
    if resolved_sha is None:
        pointer = await (
            env.APP_DB.prepare("SELECT source_sha FROM cf_desktop_preview_pointers WHERE slug = ? LIMIT 1")
            .bind(slug)
            .first()
        )
        resolved_sha = _preview_source_sha(pointer.get("source_sha")) if isinstance(pointer, dict) else None
    if resolved_sha is None:
        return None
    row = (
        await env.APP_DB.prepare(_PREVIEW_SELECT + "WHERE slug = ? AND source_sha = ? LIMIT 1")
        .bind(slug, resolved_sha)
        .first()
    )
    return _preview_manifest(row, slug=slug, source_sha=resolved_sha)


def _preview_landing_html(manifest: dict[str, object]) -> str:
    app_name = html.escape(str(manifest["app_name"]), quote=True)
    slug = html.escape(str(manifest["slug"]), quote=True)
    source_sha = html.escape(str(manifest["source_sha"]), quote=True)
    built_at = html.escape(str(manifest["built_at"]), quote=True)
    dmg_url = html.escape(str(manifest["dmg_url"]), quote=True)
    notes = manifest.get("notes")
    notes_html = f'<p class="notes">{html.escape(str(notes), quote=True)}</p>' if notes else ""
    return f"""<!DOCTYPE html>
<html lang="en">
<head>
    <meta charset="utf-8">
    <meta name="viewport" content="width=device-width, initial-scale=1">
    <meta http-equiv="refresh" content="2;url={dmg_url}">
    <title>Download {app_name} for macOS</title>
    <style>
        * {{ box-sizing: border-box; }}
        body {{ margin: 0; min-height: 100vh; display: grid; place-items: center; background: #0a0a0a;
               color: #f5f5f5; font-family: -apple-system, BlinkMacSystemFont, "Segoe UI", sans-serif; }}
        main {{ width: min(620px, calc(100% - 48px)); padding: 40px; border: 1px solid #2a2a2a; border-radius: 16px;
               background: #121212; text-align: center; }}
        h1 {{ margin: 0 0 12px; font-size: 28px; }}
        p {{ color: #b6b6b6; line-height: 1.5; }}
        code {{ display: block; overflow-wrap: anywhere; padding: 12px; border-radius: 8px; background: #1c1c1c;
               color: #e8e8e8; font-size: 13px; }}
        a {{ color: #ffffff; }}
        .notes {{ white-space: pre-wrap; }}
        .meta {{ margin-top: 24px; text-align: left; font-size: 13px; color: #909090; }}
    </style>
</head>
<body>
    <main>
        <h1>Downloading {app_name}</h1>
        <p>Your macOS preview download should start automatically.</p>
        <p><a href="{dmg_url}">Download the preview DMG</a></p>
        {notes_html}
        <div class="meta">
            <p>Preview branch: <strong>{slug}</strong></p>
            <p>Approved source commit:</p>
            <code>{source_sha}</code>
            <p>Build time: {built_at}</p>
        </div>
    </main>
</body>
</html>"""


async def _serve_preview(request: Request, slug: str, source_sha: str | None = None) -> HTMLResponse:
    normalized_slug = _preview_slug(slug)
    normalized_sha = _preview_source_sha(source_sha) if source_sha is not None else None
    if normalized_slug is None or (source_sha is not None and normalized_sha is None):
        raise HTTPException(status_code=404, detail="Preview not found")
    try:
        manifest = await _preview_from_d1(request, normalized_slug, normalized_sha)
    except Exception as exc:
        raise HTTPException(status_code=503, detail="Preview unavailable") from exc
    if manifest is None:
        raise HTTPException(status_code=404, detail="Preview not found")
    return HTMLResponse(_preview_landing_html(manifest), headers={"Cache-Control": "no-store"})


@router.get("/v2/desktop/previews/{slug}")
async def download_current_desktop_preview(request: Request, slug: str):
    """Serve the current approved preview for one branch slug."""
    return await _serve_preview(request, slug)


@router.get("/v2/desktop/previews/{slug}/{source_sha}")
async def download_immutable_desktop_preview(request: Request, slug: str, source_sha: str):
    """Serve one immutable approved preview artifact by its full source SHA."""
    return await _serve_preview(request, slug, source_sha)


@router.get("/v2/desktop/update-feed/windows")
async def get_windows_update_feed(request: Request, channel: str = Query(default="stable")):
    """Return the immutable Windows electron-updater feed directory.

    Stable never falls through to beta. A beta request may use the stable feed
    while the beta slot is empty, matching the legacy Windows client contract.
    The feed URL is an explicit projection field; installer URLs are never
    guessed across platforms.
    """
    channel = _channel(channel)
    try:
        releases = await _live_releases(request)
    except Exception as exc:
        raise HTTPException(status_code=500, detail="Failed to fetch releases") from exc

    served_channel = channel
    release = _pick_channel(releases, channel)
    if release is None and channel == "beta":
        release = _pick_channel(releases, "stable")
        if release is not None:
            served_channel = "stable"
            record_fallback(
                component="other",
                from_mode="desktop_windows_update_feed_beta",
                to_mode="desktop_windows_update_feed_stable",
                reason="other",
                outcome="recovered",
            )
    feed_url = _https_url(release.get("windows_feed_url")) if release else None
    if release is None or feed_url is None:
        raise HTTPException(
            status_code=404,
            detail=f"No Windows update feed found for channel: {channel}",
            headers={"Cache-Control": "no-store"},
        )
    return JSONResponse(
        {
            "requested_channel": channel,
            "served_channel": served_channel,
            "version": release["version"],
            "feed_url": feed_url,
        },
        headers={"Cache-Control": "no-store"},
    )


@router.get("/v2/desktop/download/windows")
async def download_windows_desktop(request: Request, channel: str = Query(default="stable")):
    _channel(channel)
    try:
        releases = await _live_releases(request)
    except Exception as exc:
        raise HTTPException(status_code=500, detail="Failed to fetch releases") from exc
    if not releases:
        raise HTTPException(status_code=404, detail="No live desktop releases found for platform: windows")
    served_channel = channel
    release = _pick_channel(releases, channel)
    if release is None:
        served_channel = "beta" if channel == "stable" else "stable"
        release = _pick_channel(releases, served_channel)
    if release is None:
        raise HTTPException(status_code=404, detail=f"No installer found for platform windows, channel: {channel}")
    notice = (
        ""
        if served_channel == channel
        else f"No {channel} build is published right now — serving the latest {served_channel} release instead."
    )
    return HTMLResponse(
        _download_landing_html(
            _manual_download_url(release),
            platform="windows",
            channel=served_channel,
            version=str(release["version"]),
            notice=notice,
        )
    )


@router.get("/v2/desktop/update-policy")
async def get_update_policy(
    request: Request, platform: str = Query(default="macos"), current_build: int | None = Query(default=None, ge=0)
):
    if platform not in VALID_PLATFORMS:
        raise HTTPException(status_code=422, detail="Invalid platform")
    try:
        row = (
            await request.scope["env"]
            .APP_DB.prepare(
                "SELECT id, active, severity, maximum_build_number, latest_build_number, title, message, cta_text, "
                "download_url, can_dismiss, platforms_json FROM cf_desktop_update_policy WHERE id = 'current'"
            )
            .first()
        )
    except Exception:
        return _default_policy()
    return _policy(row if isinstance(row, dict) else None, platform=platform, current_build=current_build)

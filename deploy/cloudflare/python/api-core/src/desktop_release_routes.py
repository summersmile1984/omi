"""Public desktop update feeds backed by the Cloudflare D1 projection.

The release pipeline owns artifact publication and promotion. This Worker only
serves signed metadata that has been explicitly projected into D1; an empty
projection therefore fails closed for downloads and latest-version lookups.
"""

from __future__ import annotations

import html
import json
from urllib.parse import urlparse

from fastapi import APIRouter, HTTPException, Query, Request
from fastapi.responses import RedirectResponse, Response

router = APIRouter()

DEFAULT_DOWNLOAD_URL = "https://api.omi.me/v2/desktop/download/latest?channel=stable"
VALID_PLATFORMS = {"macos", "windows", "linux"}
VALID_SEVERITIES = {"none", "banner", "required"}


def _https_url(value: object, default: str | None = None) -> str | None:
    candidate = value.strip() if isinstance(value, str) else ""
    parsed = urlparse(candidate)
    if parsed.scheme.lower() not in {"http", "https"} or not parsed.netloc:
        return default
    return candidate


def _manual_download_url(release: dict[str, object]) -> str:
    explicit = release.get("manual_download_url")
    if isinstance(explicit, str) and explicit:
        return explicit
    download_url = str(release["download_url"])
    if download_url.endswith("/Omi.zip"):
        return f"{download_url[:-len('Omi.zip')]}Omi.dmg"
    return download_url


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
    }


async def _live_releases(request: Request) -> list[dict[str, object]]:
    rows = (
        await request.scope["env"]
        .APP_DB.prepare(
            "SELECT version, build_number, download_url, manual_download_url, ed_signature, published_at, "
            "changelog_json, is_live, is_critical, channel "
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

#!/usr/bin/env python3
"""Scheduled release detection with GitHub issue notifications."""

from __future__ import annotations

import argparse
import http.cookiejar
import hashlib
import html.parser
import json
import os
import re
import shutil
import subprocess
import sys
from dataclasses import dataclass
from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import Any, Final
from urllib import error, parse, request


MARKER_START: Final[str] = "<!-- release-detection-state"
MARKER_END: Final[str] = "-->"
DEFAULT_CONFIG_PATH: Final[Path] = Path("targets.json")
GITHUB_API_BASE: Final[str] = "https://api.github.com"
GITHUB_API_ACCEPT: Final[str] = "application/vnd.github+json"
GITHUB_API_VERSION: Final[str] = "2022-11-28"
GITHUB_UPLOADS_BASE: Final[str] = "https://uploads.github.com"
RG_ADGUARD_URL: Final[str] = "https://store.rg-adguard.net/api/GetFiles"
RG_ADGUARD_HOME_URL: Final[str] = "https://store.rg-adguard.net/"
DOWNLOADS_DIR: Final[Path] = Path("downloads")
MARKETPLACE_API_URL: Final[str] = (
    "https://marketplace.visualstudio.com/_apis/public/gallery/extensionquery"
)
MARKETPLACE_ACCEPT: Final[str] = "application/json;api-version=7.2-preview.1"
MICROSOFT_STORE_DISPLAY_CATALOG_URL: Final[str] = (
    "https://displaycatalog.mp.microsoft.com/v7.0/products"
)
MICROSOFT_STORE_PACKAGE_VERSION_PATTERN: Final[re.Pattern[str]] = re.compile(
    r"^[^_]+_([0-9]+(?:\.[0-9]+){3})_"
)
SHA1_PATTERN: Final[re.Pattern[str]] = re.compile(r"\b[0-9a-fA-F]{40}\b")
MSSTORE_CDN_HOST: Final[str] = "dl.delivery.mp.microsoft.com"


@dataclass(frozen=True)
class ChannelRelease:
    """Release details for a single channel."""

    version: str
    last_updated: str


@dataclass(frozen=True)
class TargetSnapshot:
    """Latest detected versions for a target."""

    target_id: str
    name: str
    source_url: str
    channels: dict[str, ChannelRelease]


@dataclass(frozen=True)
class MicrosoftStoreCatalogRelease:
    """Release details parsed from the Microsoft Store DisplayCatalog API."""

    version: str
    last_updated: str | None


@dataclass(frozen=True)
class MicrosoftStoreDownload:
    """Resolved Microsoft Store package download metadata."""

    filename: str
    url: str
    sha1: str


@dataclass(frozen=True)
class HtmlLink:
    """HTML anchor metadata extracted from a table row."""

    href: str
    text: str


class RgAdguardTableParser(html.parser.HTMLParser):
    """Extract table rows and links from the rg-adguard HTML response."""

    def __init__(self) -> None:
        """Initialize the parser state.

        @param None
        @returns None
        """

        super().__init__()
        self.rows: list[list[str]] = []
        self.row_links: list[list[HtmlLink]] = []
        self._current_row: list[str] | None = None
        self._current_row_links: list[HtmlLink] | None = None
        self._current_cell: list[str] | None = None
        self._current_link_href: str | None = None
        self._current_link_text: list[str] | None = None

    def handle_starttag(
        self, tag: str, attrs: list[tuple[str, str | None]]
    ) -> None:
        """Track table rows, cells, and links.

        @param tag HTML tag name.
        @param attrs HTML attributes for the tag.
        @returns None
        """

        normalized = tag.lower()
        if normalized == "tr":
            self._current_row = []
            self._current_row_links = []
        elif normalized in {"td", "th"} and self._current_row is not None:
            self._current_cell = []
        elif normalized == "a":
            href = dict(attrs).get("href")
            if href:
                self._current_link_href = href
                self._current_link_text = []

    def handle_data(self, data: str) -> None:
        """Collect visible cell text.

        @param data HTML text node.
        @returns None
        """

        if self._current_cell is not None:
            self._current_cell.append(data)
        if self._current_link_text is not None:
            self._current_link_text.append(data)

    def handle_endtag(self, tag: str) -> None:
        """Finalize table rows and cells.

        @param tag HTML tag name.
        @returns None
        """

        normalized = tag.lower()
        if normalized == "a" and self._current_link_href is not None:
            if self._current_row_links is not None:
                text = " ".join(
                    part.strip()
                    for part in self._current_link_text or []
                    if part.strip()
                )
                self._current_row_links.append(
                    HtmlLink(href=self._current_link_href, text=text)
                )
            self._current_link_href = None
            self._current_link_text = None
        elif normalized in {"td", "th"} and self._current_cell is not None:
            assert self._current_row is not None
            cell = " ".join(part.strip() for part in self._current_cell if part.strip())
            self._current_row.append(cell)
            self._current_cell = None
        elif normalized == "tr" and self._current_row is not None:
            if self._current_row:
                self.rows.append(self._current_row)
                self.row_links.append(self._current_row_links or [])
            self._current_row = None
            self._current_row_links = None


def parse_args() -> argparse.Namespace:
    """Parse CLI arguments.

    @param None
    @returns Parsed arguments namespace.
    """

    parser = argparse.ArgumentParser(
        description="Detect new releases and notify via GitHub issues."
    )
    parser.add_argument(
        "--config",
        type=Path,
        default=DEFAULT_CONFIG_PATH,
        help="Path to the release target config file.",
    )
    parser.add_argument(
        "--dry-run",
        action="store_true",
        help="Print planned actions without touching GitHub issues.",
    )
    return parser.parse_args()


def load_config(config_path: Path) -> list[dict[str, object]]:
    """Load and validate the target config.

    @param config_path Path to the JSON config file.
    @returns Configured target entries.
    """

    payload = json.loads(config_path.read_text(encoding="utf-8"))
    targets = payload.get("targets")
    if not isinstance(targets, list) or not targets:
        raise ValueError("targets.json must contain a non-empty 'targets' array.")
    return targets


def http_json(
    method: str,
    url: str,
    *,
    headers: dict[str, str] | None = None,
    body: dict[str, object] | None = None,
) -> dict[str, object]:
    """Send an HTTP request and decode a JSON response.

    @param method HTTP method.
    @param url Target URL.
    @param headers Request headers.
    @param body Optional JSON body.
    @returns Decoded JSON object.
    """

    encoded_body = None
    request_headers = {
        "Accept": "application/json",
        "User-Agent": "release-detection-ci",
    }
    if headers:
        request_headers.update(headers)
    if body is not None:
        encoded_body = json.dumps(body).encode("utf-8")
        request_headers["Content-Type"] = "application/json"

    req = request.Request(url, data=encoded_body, headers=request_headers, method=method)
    try:
        with request.urlopen(req) as response:
            return json.loads(response.read().decode("utf-8"))
    except error.HTTPError as exc:
        payload = exc.read().decode("utf-8", errors="replace")
        raise RuntimeError(f"HTTP {exc.code} {url}: {payload}") from exc


def http_text(
    method: str, url: str, *, headers: dict[str, str] | None = None
) -> str:
    """Send an HTTP request and decode a text response.

    @param method HTTP method.
    @param url Target URL.
    @param headers Request headers.
    @returns Decoded text body.
    """

    request_headers = {
        "Accept": "text/html,application/xhtml+xml,application/xml;q=0.9,*/*;q=0.8",
        "User-Agent": "release-detection-ci",
    }
    if headers:
        request_headers.update(headers)

    req = request.Request(url, headers=request_headers, method=method)
    try:
        with request.urlopen(req) as response:
            charset = response.headers.get_content_charset("utf-8")
            return response.read().decode(charset, errors="replace")
    except error.HTTPError as exc:
        payload = exc.read().decode("utf-8", errors="replace")
        raise RuntimeError(f"HTTP {exc.code} {url}: {payload}") from exc


def http_form_text(
    method: str,
    url: str,
    *,
    form: dict[str, str],
    headers: dict[str, str] | None = None,
) -> str:
    """Send a form request and decode a text response.

    @param method HTTP method.
    @param url Target URL.
    @param form Form fields.
    @param headers Request headers.
    @returns Decoded text body.
    """

    request_headers = {
        "Accept": "text/html,application/xhtml+xml,application/xml;q=0.9,*/*;q=0.8",
        "Content-Type": "application/x-www-form-urlencoded",
        "User-Agent": "release-detection-ci",
    }
    if headers:
        request_headers.update(headers)

    encoded_body = parse.urlencode(form).encode("utf-8")
    req = request.Request(
        url, data=encoded_body, headers=request_headers, method=method
    )
    try:
        with request.urlopen(req) as response:
            charset = response.headers.get_content_charset("utf-8")
            return response.read().decode(charset, errors="replace")
    except error.HTTPError as exc:
        payload = exc.read().decode("utf-8", errors="replace")
        raise RuntimeError(f"HTTP {exc.code} {url}: {payload}") from exc


def rg_adguard_form_text(form: dict[str, str]) -> str:
    """Submit an rg-adguard form request with browser-like headers.

    @param form Form fields for the rg-adguard file resolver.
    @returns Decoded HTML response.
    """

    cookie_jar = http.cookiejar.CookieJar()
    opener = request.build_opener(request.HTTPCookieProcessor(cookie_jar))
    browser_headers = {
        "Accept": "text/html,application/xhtml+xml,application/xml;q=0.9,*/*;q=0.8",
        "Accept-Language": "en-US,en;q=0.9",
        "User-Agent": (
            "Mozilla/5.0 (Windows NT 10.0; Win64; x64) "
            "AppleWebKit/537.36 (KHTML, like Gecko) "
            "Chrome/124.0.0.0 Safari/537.36"
        ),
    }

    try:
        opener.open(request.Request(RG_ADGUARD_HOME_URL, headers=browser_headers))
        post_headers = {
            **browser_headers,
            "Content-Type": "application/x-www-form-urlencoded",
            "Origin": "https://store.rg-adguard.net",
            "Referer": RG_ADGUARD_HOME_URL,
        }
        req = request.Request(
            RG_ADGUARD_URL,
            data=parse.urlencode(form).encode("utf-8"),
            headers=post_headers,
            method="POST",
        )
        with opener.open(req) as response:
            charset = response.headers.get_content_charset("utf-8")
            return response.read().decode(charset, errors="replace")
    except error.HTTPError as exc:
        payload = exc.read().decode("utf-8", errors="replace")
        raise RuntimeError(f"HTTP {exc.code} {RG_ADGUARD_URL}: {payload}") from exc


def http_bytes(url: str, *, headers: dict[str, str] | None = None) -> bytes:
    """Download a binary HTTP response.

    @param url Target URL.
    @param headers Request headers.
    @returns Response bytes.
    """

    request_headers = {"User-Agent": "release-detection-ci"}
    if headers:
        request_headers.update(headers)
    req = request.Request(url, headers=request_headers, method="GET")
    try:
        with request.urlopen(req) as response:
            return response.read()
    except error.HTTPError as exc:
        payload = exc.read().decode("utf-8", errors="replace")
        raise RuntimeError(f"HTTP {exc.code} {url}: {payload}") from exc


def github_json(
    method: str,
    url: str,
    token: str,
    *,
    body: dict[str, object] | None = None,
) -> Any:
    """Send an authenticated GitHub JSON request.

    @param method HTTP method.
    @param url Target URL.
    @param token GitHub token.
    @param body Optional JSON body.
    @returns Decoded JSON payload.
    """

    return http_json(method, url, headers=github_headers(token), body=body)


def parse_iso8601(value: str) -> datetime:
    """Normalize ISO-8601 values from upstream APIs.

    @param value Source timestamp.
    @returns Parsed UTC datetime.
    """

    return datetime.fromisoformat(value.replace("Z", "+00:00")).astimezone(timezone.utc)


def to_iso8601(value: datetime) -> str:
    """Format a UTC timestamp for persisted state.

    @param value UTC datetime.
    @returns Normalized timestamp string.
    """

    return value.astimezone(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")


def query_vs_code_marketplace(target: dict[str, object]) -> TargetSnapshot:
    """Query the VS Code Marketplace gallery API.

    @param target Target config entry.
    @returns Latest channel versions for the target.
    """

    source = target.get("source")
    if not isinstance(source, dict):
        raise ValueError(f"Target '{target.get('id')}' is missing a 'source' object.")

    publisher = str(source["publisher"])
    extension = str(source["extension"])
    fully_qualified_name = f"{publisher}.{extension}"

    response = http_json(
        "POST",
        MARKETPLACE_API_URL,
        headers={"Accept": MARKETPLACE_ACCEPT},
        body={
            "filters": [
                {
                    "criteria": [{"filterType": 7, "value": fully_qualified_name}],
                    "pageNumber": 1,
                    "pageSize": 1,
                    "sortBy": 0,
                    "sortOrder": 0,
                }
            ],
            "assetTypes": [],
            "flags": 914,
        },
    )

    results = response.get("results")
    if not isinstance(results, list) or not results:
        raise ValueError(f"Marketplace returned no results for '{fully_qualified_name}'.")

    extensions = results[0].get("extensions")
    if not isinstance(extensions, list) or not extensions:
        raise ValueError(
            f"Marketplace returned no extension payload for '{fully_qualified_name}'."
        )

    extension_payload = extensions[0]
    versions = extension_payload.get("versions")
    if not isinstance(versions, list) or not versions:
        raise ValueError(
            f"Marketplace returned no versions for '{fully_qualified_name}'."
        )

    latest_by_channel: dict[str, tuple[datetime, ChannelRelease]] = {}
    for version_entry in versions:
        if not isinstance(version_entry, dict):
            continue
        version = version_entry.get("version")
        updated = version_entry.get("lastUpdated")
        if not isinstance(version, str) or not isinstance(updated, str):
            continue

        properties = version_entry.get("properties")
        prerelease = False
        if isinstance(properties, list):
            for prop in properties:
                if not isinstance(prop, dict):
                    continue
                if (
                    prop.get("key") == "Microsoft.VisualStudio.Code.PreRelease"
                    and str(prop.get("value")).lower() == "true"
                ):
                    prerelease = True
                    break

        channel = "prerelease" if prerelease else "stable"
        parsed_updated = parse_iso8601(updated)
        existing = latest_by_channel.get(channel)
        if existing is None or parsed_updated > existing[0]:
            latest_by_channel[channel] = (
                parsed_updated,
                ChannelRelease(version=version, last_updated=to_iso8601(parsed_updated)),
            )

    include_stable = bool(source.get("includeStable", True))
    include_prerelease = bool(source.get("includePrerelease", False))
    channels: dict[str, ChannelRelease] = {}
    if include_stable and "stable" in latest_by_channel:
        channels["stable"] = latest_by_channel["stable"][1]
    if include_prerelease and "prerelease" in latest_by_channel:
        channels["prerelease"] = latest_by_channel["prerelease"][1]

    if not channels:
        raise ValueError(
            f"No channels matched the configured policy for '{fully_qualified_name}'."
        )

    source_url = str(
        source.get(
            "itemUrl",
            f"https://marketplace.visualstudio.com/items?itemName={fully_qualified_name}",
        )
    )
    return TargetSnapshot(
        target_id=str(target["id"]),
        name=str(target["name"]),
        source_url=source_url,
        channels=channels,
    )


def extract_embedded_json(html: str, variable_name: str) -> dict[str, object]:
    """Extract a JSON object assigned to a window variable in HTML.

    @param html HTML document text.
    @param variable_name Window variable name.
    @returns Parsed JSON object.
    """

    pattern = rf"window\.{re.escape(variable_name)}\s*=\s*(\{{.*?\}});"
    match = re.search(pattern, html, flags=re.DOTALL)
    if match is None:
        raise ValueError(f"Could not find window.{variable_name} in the HTML payload.")
    return json.loads(match.group(1))


def resolve_microsoft_store_product_id(
    target: dict[str, object], source: dict[str, object]
) -> str:
    """Resolve a Microsoft Store product ID from config or product URL.

    @param target Target config entry.
    @param source Target source config.
    @returns Microsoft Store product ID.
    """

    configured_product_id = source.get("productId", target.get("productId"))
    if isinstance(configured_product_id, str) and configured_product_id:
        return configured_product_id

    product_url = str(source["productUrl"])
    parsed_url = parse.urlparse(product_url)
    match = re.search(r"/detail/([^/?#]+)", parsed_url.path, flags=re.IGNORECASE)
    if match is None:
        raise ValueError(
            f"Microsoft Store product URL did not include a /detail/<id> path: '{product_url}'."
        )
    return parse.unquote(match.group(1))


def is_allowed_microsoft_cdn_url(url: str) -> bool:
    """Validate that a package URL points at the Microsoft CDN host.

    @param url Candidate download URL.
    @returns True when the host is allowed.
    """

    host = parse.urlparse(url).hostname
    if host is None:
        return False
    normalized = host.lower()
    return normalized == MSSTORE_CDN_HOST or normalized.endswith(
        f".{MSSTORE_CDN_HOST}"
    )


def parse_rg_adguard_downloads(html: str) -> list[MicrosoftStoreDownload]:
    """Parse rg-adguard HTML into MSIX download candidates.

    @param html HTML response from rg-adguard.
    @returns MSIX download metadata rows.
    """

    parser = RgAdguardTableParser()
    parser.feed(html)
    candidates: list[MicrosoftStoreDownload] = []

    for index, row in enumerate(parser.rows):
        row_text = " ".join(row)
        if ".msix" not in row_text.lower():
            continue
        sha1_match = SHA1_PATTERN.search(row_text)
        if sha1_match is None:
            continue

        row_links = parser.row_links[index] if index < len(parser.row_links) else []
        link = next(
            (candidate for candidate in row_links if ".msix" in candidate.text.lower()),
            None,
        )
        if link is None:
            continue

        filename = link.text.strip()
        if not filename.lower().endswith(".msix"):
            filename_match = re.search(r"OpenAI\.Codex_[^\s<>\"']+?\.msix", row_text)
            if filename_match is None:
                continue
            filename = filename_match.group(0)

        candidates.append(
            MicrosoftStoreDownload(
                filename=filename,
                url=link.href,
                sha1=sha1_match.group(0).upper(),
            )
        )

    return candidates


def resolve_microsoft_store_download(
    product_id: str, version: str
) -> MicrosoftStoreDownload:
    """Resolve an MSIX package URL through rg-adguard.

    @param product_id Microsoft Store product ID.
    @param version Package version to download.
    @returns Selected MSIX download metadata.
    """

    html = rg_adguard_form_text(
        {
            "type": "ProductId",
            "url": product_id,
            "ring": "Retail",
            "lang": "en-US",
        },
    )
    candidates = parse_rg_adguard_downloads(html)
    expected_filename = f"OpenAI.Codex_{version}_x64__2p2nqsd0c76g0.msix"
    selected = next(
        (candidate for candidate in candidates if candidate.filename == expected_filename),
        None,
    )
    if selected is None:
        selected = next(
            (
                candidate
                for candidate in candidates
                if candidate.filename.startswith(f"OpenAI.Codex_{version}_")
                and candidate.filename.endswith(".msix")
            ),
            None,
        )
    if selected is None:
        available = ", ".join(candidate.filename for candidate in candidates) or "none"
        raise ValueError(
            f"rg-adguard did not return a matching OpenAI Codex MSIX for version {version}; available: {available}"
        )
    if not is_allowed_microsoft_cdn_url(selected.url):
        raise ValueError(
            f"Resolved MSIX URL is not hosted on the allowed Microsoft CDN: {selected.url}"
        )
    return selected


def ensure_downloaded_msix(download: MicrosoftStoreDownload) -> Path:
    """Download the selected MSIX into downloads/.

    @param download Selected download metadata.
    @returns Local MSIX path.
    """

    DOWNLOADS_DIR.mkdir(exist_ok=True)
    destination = DOWNLOADS_DIR / download.filename
    if destination.exists():
        print(f"Using existing download: {destination}")
        return destination

    payload = http_bytes(download.url)
    destination.write_bytes(payload)
    print(f"Downloaded MSIX: {destination} ({len(payload)} bytes)")
    return destination


def verify_msix_sha1(path: Path, expected_sha1: str) -> str:
    """Verify a downloaded MSIX SHA-1 hash.

    @param path Local MSIX path.
    @param expected_sha1 Expected SHA-1 from rg-adguard.
    @returns Actual uppercase SHA-1.
    """

    digest = hashlib.sha1(path.read_bytes()).hexdigest().upper()
    if digest != expected_sha1.upper():
        raise ValueError(
            f"MSIX SHA-1 mismatch for {path}: expected {expected_sha1.upper()}, got {digest}"
        )
    return digest


def verify_msix_authenticode(path: Path) -> str:
    """Verify a downloaded MSIX Authenticode signature on Windows.

    @param path Local MSIX path.
    @returns Authenticode status string.
    """

    powershell = shutil.which("pwsh") or shutil.which("powershell")
    if os.name != "nt" or powershell is None:
        raise RuntimeError(
            "Microsoft Store MSIX publishing requires Windows with PowerShell for Get-AuthenticodeSignature."
        )

    command = [
        powershell,
        "-NoProfile",
        "-Command",
        "$sig = Get-AuthenticodeSignature -LiteralPath $args[0]; "
        "if ($sig.Status -ne 'Valid') { "
        "Write-Error \"AuthenticodeSignature status is $($sig.Status)\"; exit 1 "
        "} "
        "Write-Output $sig.Status",
        str(path),
    ]
    completed = subprocess.run(
        command,
        check=False,
        capture_output=True,
        text=True,
    )
    if completed.returncode != 0:
        stderr = completed.stderr.strip() or completed.stdout.strip()
        raise RuntimeError(f"Authenticode verification failed for {path}: {stderr}")
    return completed.stdout.strip()


def download_and_verify_microsoft_store_msix(
    target: dict[str, object], version: str
) -> tuple[Path, MicrosoftStoreDownload, str, str]:
    """Resolve, download, and verify a Microsoft Store Codex MSIX.

    @param target Target config entry.
    @param version Package version.
    @returns Local path, download metadata, SHA-1, and signature status.
    """

    source = target.get("source")
    if not isinstance(source, dict):
        raise ValueError(f"Target '{target.get('id')}' is missing a 'source' object.")

    product_id = resolve_microsoft_store_product_id(target, source)
    download = resolve_microsoft_store_download(product_id, version)
    path = ensure_downloaded_msix(download)
    sha1 = verify_msix_sha1(path, download.sha1)
    signature_status = verify_msix_authenticode(path)
    print(
        "Verified MSIX: "
        f"path={path}, bytes={path.stat().st_size}, sha1={sha1}, "
        f"signature={signature_status}, source={download.url}"
    )
    return path, download, sha1, signature_status


def build_microsoft_store_catalog_url(
    product_id: str, source: dict[str, object]
) -> str:
    """Build a Microsoft Store DisplayCatalog product metadata URL.

    @param product_id Microsoft Store product ID.
    @param source Target source config.
    @returns DisplayCatalog request URL.
    """

    product_url = str(source["productUrl"])
    query = parse.parse_qs(parse.urlparse(product_url).query)
    market = str(source.get("market") or next(iter(query.get("gl", [])), "US"))
    languages = str(
        source.get("languages") or next(iter(query.get("hl", [])), "en-US")
    )
    return (
        f"{MICROSOFT_STORE_DISPLAY_CATALOG_URL}?"
        f"{parse.urlencode({'bigIds': product_id, 'market': market, 'languages': languages})}"
    )


def extract_microsoft_store_package_version(package_full_name: object) -> str | None:
    """Extract an MSIX package version from a PackageFullName value.

    @param package_full_name Candidate PackageFullName value.
    @returns Parsed package version, or None when the value is not an MSIX name.
    """

    if not isinstance(package_full_name, str):
        return None
    match = MICROSOFT_STORE_PACKAGE_VERSION_PATTERN.search(package_full_name)
    if match is None:
        return None
    return match.group(1)


def normalize_microsoft_store_timestamp(value: object) -> str | None:
    """Normalize a Microsoft Store timestamp when present and parseable.

    @param value Candidate timestamp value.
    @returns Normalized UTC timestamp, or None when unavailable.
    """

    if not isinstance(value, str) or not value:
        return None
    return to_iso8601(parse_iso8601(value))


def select_microsoft_store_timestamp(*containers: object) -> str | None:
    """Select the first package, SKU, or product update timestamp.

    @param containers Metadata objects ordered by update timestamp precedence.
    @returns Normalized UTC timestamp, or None when none is available.
    """

    for container in containers:
        if not isinstance(container, dict):
            continue
        for key in ("LastUpdateDate", "LastModifiedDate"):
            normalized = normalize_microsoft_store_timestamp(container.get(key))
            if normalized is not None:
                return normalized
    return None


def parse_version_tuple(version: str) -> tuple[int, ...]:
    """Convert a dotted numeric version to a comparable tuple.

    @param version Dotted numeric version string.
    @returns Integer tuple suitable for version ordering.
    """

    return tuple(int(segment) for segment in version.split("."))


def extract_microsoft_store_catalog_release(
    catalog: dict[str, object],
) -> MicrosoftStoreCatalogRelease | None:
    """Extract the best MSIX release from DisplayCatalog metadata.

    @param catalog DisplayCatalog response payload.
    @returns Parsed package release, or None when no package version is available.
    """

    candidates: list[MicrosoftStoreCatalogRelease] = []
    products = catalog.get("Products")
    if not isinstance(products, list):
        return None

    for product in products:
        if not isinstance(product, dict):
            continue
        availabilities = product.get("DisplaySkuAvailabilities")
        if not isinstance(availabilities, list):
            continue
        for availability in availabilities:
            if not isinstance(availability, dict):
                continue
            sku = availability.get("Sku")
            if not isinstance(sku, dict):
                continue
            properties = sku.get("Properties")
            if not isinstance(properties, dict):
                continue
            packages = properties.get("Packages")
            if not isinstance(packages, list):
                continue
            for package in packages:
                if not isinstance(package, dict):
                    continue
                version = extract_microsoft_store_package_version(
                    package.get("PackageFullName")
                )
                if version is None:
                    continue
                candidates.append(
                    MicrosoftStoreCatalogRelease(
                        version=version,
                        last_updated=select_microsoft_store_timestamp(
                            package, sku, product
                        ),
                    )
                )

    if not candidates:
        return None

    return max(
        candidates,
        key=lambda candidate: (
            candidate.last_updated or "",
            parse_version_tuple(candidate.version),
        ),
    )


def query_microsoft_store_web(target: dict[str, object]) -> TargetSnapshot:
    """Query product metadata embedded in a Microsoft Store web page.

    @param target Target config entry.
    @returns Latest release signal for the target.
    """

    source = target.get("source")
    if not isinstance(source, dict):
        raise ValueError(f"Target '{target.get('id')}' is missing a 'source' object.")

    product_url = str(source["productUrl"])
    html = http_text("GET", product_url)
    page_metadata = extract_embedded_json(html, "pageMetadata")
    product_id = resolve_microsoft_store_product_id(target, source)
    catalog_url = build_microsoft_store_catalog_url(product_id, source)
    catalog_release = extract_microsoft_store_catalog_release(
        http_json("GET", catalog_url)
    )

    package_last_update = page_metadata.get("packageLastUpdateDateUtc")
    release_date = page_metadata.get("releaseDateUtc")
    page_package_update = normalize_microsoft_store_timestamp(package_last_update)

    if catalog_release is not None:
        catalog_last_updated = catalog_release.last_updated or page_package_update
        if catalog_last_updated is None:
            raise ValueError(
                f"Microsoft Store metadata did not expose a package update timestamp for '{product_url}'."
            )
        return TargetSnapshot(
            target_id=str(target["id"]),
            name=str(target["name"]),
            source_url=product_url,
            channels={
                "stable": ChannelRelease(
                    version=catalog_release.version,
                    last_updated=catalog_last_updated,
                )
            },
        )

    if not isinstance(package_last_update, str) or not package_last_update:
        raise ValueError(
            f"Microsoft Store page did not expose packageLastUpdateDateUtc for '{product_url}'."
        )

    signal = page_package_update
    if signal is None:
        raise ValueError(
            f"Microsoft Store page exposed an invalid packageLastUpdateDateUtc for '{product_url}'."
        )

    version_suffix = ""
    if isinstance(release_date, str) and release_date:
        version_suffix = f" (releaseDateUtc={to_iso8601(parse_iso8601(release_date))})"

    return TargetSnapshot(
        target_id=str(target["id"]),
        name=str(target["name"]),
        source_url=product_url,
        channels={
            "stable": ChannelRelease(
                version=f"packageLastUpdateDateUtc={signal}{version_suffix}",
                last_updated=signal,
            )
        },
    )


def query_github_releases(
    target: dict[str, object], token: str = ""
) -> TargetSnapshot:
    """Query the latest published full release from GitHub Releases.

    @param target Target config entry.
    @param token Optional GitHub token for authenticated upstream API requests.
    @returns Latest stable release for the target repository.
    """

    source = target.get("source")
    if not isinstance(source, dict):
        raise ValueError(f"Target '{target.get('id')}' is missing a 'source' object.")

    owner = str(source["owner"])
    repo = str(source["repo"])
    releases_url = str(
        source.get("releasesUrl", f"https://github.com/{owner}/{repo}/releases")
    )
    response = http_json(
        "GET",
        f"{GITHUB_API_BASE}/repos/{owner}/{repo}/releases/latest",
        headers=github_headers(token),
    )

    if response.get("draft") is True or response.get("prerelease") is True:
        raise ValueError(
            f"GitHub latest release endpoint returned a non-stable release for '{owner}/{repo}'."
        )

    tag_name = response.get("tag_name")
    if not isinstance(tag_name, str) or not tag_name:
        raise ValueError(
            f"GitHub Releases did not expose a tag_name for '{owner}/{repo}'."
        )

    published_at = response.get("published_at")
    created_at = response.get("created_at")
    timestamp = published_at if isinstance(published_at, str) and published_at else created_at
    if not isinstance(timestamp, str) or not timestamp:
        raise ValueError(
            f"GitHub Releases did not expose a usable timestamp for '{owner}/{repo}'."
        )

    return TargetSnapshot(
        target_id=str(target["id"]),
        name=str(target["name"]),
        source_url=releases_url,
        channels={
            "stable": ChannelRelease(
                version=tag_name,
                last_updated=to_iso8601(parse_iso8601(timestamp)),
            )
        },
    )


def build_issue_state(snapshot: TargetSnapshot) -> dict[str, object]:
    """Build the persisted issue metadata.

    @param snapshot Latest detected snapshot.
    @returns Hidden state payload stored in the issue body.
    """

    return {
        "target_id": snapshot.target_id,
        "source_url": snapshot.source_url,
        "channels": {
            channel: {
                "version": release.version,
                "last_updated": release.last_updated,
            }
            for channel, release in snapshot.channels.items()
        },
    }


def extract_issue_state(body: str) -> dict[str, object] | None:
    """Extract hidden JSON state from an issue body.

    @param body Issue body.
    @returns Parsed hidden state, or None when not found.
    """

    start = body.find(MARKER_START)
    if start == -1:
        return None
    end = body.find(MARKER_END, start)
    if end == -1:
        return None
    marker_payload = body[start + len(MARKER_START) : end].strip()
    if not marker_payload:
        return None
    return json.loads(marker_payload)


def format_issue_body(snapshot: TargetSnapshot) -> str:
    """Render the tracking issue body.

    @param snapshot Latest detected snapshot.
    @returns Markdown body text.
    """

    visible_lines = [
        "# Release Tracking",
        "",
        f"- Target: `{snapshot.name}`",
        f"- Source: {snapshot.source_url}",
        "- Notification mode: GitHub issue comments",
        "- Subscribe: watch this repository or subscribe to this issue to receive emails.",
        "",
        "## Recorded Versions",
    ]

    for channel, release in sorted(snapshot.channels.items()):
        visible_lines.append(
            f"- `{channel}`: `{release.version}` (`last_updated={release.last_updated}`)"
        )

    hidden_state = json.dumps(build_issue_state(snapshot), ensure_ascii=True, indent=2)
    visible_lines.extend(
        [
            "",
            MARKER_START,
            hidden_state,
            MARKER_END,
        ]
    )
    return "\n".join(visible_lines)


def format_change_comment(
    snapshot: TargetSnapshot,
    changes: list[tuple[str, str | None, str]],
) -> str:
    """Render a comment for newly detected versions.

    @param snapshot Latest detected snapshot.
    @param changes Changed channels with previous and new versions.
    @returns Markdown comment text.
    """

    lines = [
        "New release detected.",
        "",
        f"- Target: `{snapshot.name}`",
        f"- Source: {snapshot.source_url}",
        "",
        "## Changes",
    ]
    for channel, previous, current in changes:
        previous_value = previous or "none"
        lines.append(f"- `{channel}`: `{previous_value}` -> `{current}`")
    return "\n".join(lines)


def github_headers(token: str = "") -> dict[str, str]:
    """Build GitHub API headers.

    @param token GitHub token.
    @returns HTTP headers for the GitHub REST API.
    """

    headers = {
        "Accept": "application/vnd.github+json",
        "X-GitHub-Api-Version": "2022-11-28",
    }
    if token:
        headers["Authorization"] = f"Bearer {token}"
    return headers


def github_release_by_tag(
    owner: str, repo: str, token: str, tag_name: str
) -> dict[str, object] | None:
    """Fetch a GitHub release by tag.

    @param owner Repository owner.
    @param repo Repository name.
    @param token GitHub token.
    @param tag_name Release tag name.
    @returns Release payload, or None when not found.
    """

    url = f"{GITHUB_API_BASE}/repos/{owner}/{repo}/releases/tags/{parse.quote(tag_name, safe='')}"
    try:
        response = github_json("GET", url, token)
    except RuntimeError as exc:
        if "HTTP 404" in str(exc):
            return None
        raise
    if not isinstance(response, dict):
        raise ValueError(f"GitHub release lookup returned an unexpected payload for {tag_name}.")
    return response


def create_github_release(
    owner: str, repo: str, token: str, tag_name: str, name: str
) -> dict[str, object]:
    """Create a GitHub release for a tag.

    @param owner Repository owner.
    @param repo Repository name.
    @param token GitHub token.
    @param tag_name Release tag name.
    @param name Release display name.
    @returns Created release payload.
    """

    response = github_json(
        "POST",
        f"{GITHUB_API_BASE}/repos/{owner}/{repo}/releases",
        token,
        body={
            "tag_name": tag_name,
            "name": name,
            "draft": False,
            "prerelease": False,
            "generate_release_notes": False,
        },
    )
    if not isinstance(response, dict):
        raise ValueError(f"GitHub release creation returned an unexpected payload for {tag_name}.")
    return response


def list_github_release_assets(
    owner: str, repo: str, token: str, release_id: int
) -> list[dict[str, object]]:
    """List assets attached to a GitHub release.

    @param owner Repository owner.
    @param repo Repository name.
    @param token GitHub token.
    @param release_id GitHub release ID.
    @returns Release asset payloads.
    """

    response = github_json(
        "GET",
        f"{GITHUB_API_BASE}/repos/{owner}/{repo}/releases/{release_id}/assets?per_page=100",
        token,
    )
    if not isinstance(response, list):
        raise ValueError(f"GitHub assets API returned an unexpected payload for release {release_id}.")
    return [asset for asset in response if isinstance(asset, dict)]


def list_github_releases(owner: str, repo: str, token: str) -> list[dict[str, object]]:
    """List repository releases from GitHub.

    @param owner Repository owner.
    @param repo Repository name.
    @param token GitHub token.
    @returns Release payloads.
    """

    releases: list[dict[str, object]] = []
    page = 1
    while True:
        response = github_json(
            "GET",
            f"{GITHUB_API_BASE}/repos/{owner}/{repo}/releases?per_page=100&page={page}",
            token,
        )
        if not isinstance(response, list):
            raise ValueError("GitHub releases API returned an unexpected payload.")
        page_releases = [release for release in response if isinstance(release, dict)]
        releases.extend(page_releases)
        if len(page_releases) < 100:
            return releases
        page += 1


def github_empty(method: str, url: str, token: str) -> None:
    """Send an authenticated GitHub request that does not return JSON.

    @param method HTTP method.
    @param url Target URL.
    @param token GitHub token.
    @returns None
    """

    req = request.Request(url, headers=github_headers(token), method=method)
    try:
        with request.urlopen(req) as response:
            response.read()
    except error.HTTPError as exc:
        payload = exc.read().decode("utf-8", errors="replace")
        raise RuntimeError(f"HTTP {exc.code} {url}: {payload}") from exc


def delete_github_release(owner: str, repo: str, token: str, release_id: int) -> None:
    """Delete a GitHub release by ID.

    @param owner Repository owner.
    @param repo Repository name.
    @param token GitHub token.
    @param release_id GitHub release ID.
    @returns None
    """

    github_empty(
        "DELETE",
        f"{GITHUB_API_BASE}/repos/{owner}/{repo}/releases/{release_id}",
        token,
    )


def delete_github_tag_ref(owner: str, repo: str, token: str, tag_name: str) -> None:
    """Delete a Git tag ref, ignoring already-missing refs.

    @param owner Repository owner.
    @param repo Repository name.
    @param token GitHub token.
    @param tag_name Tag name.
    @returns None
    """

    encoded_ref = parse.quote(f"tags/{tag_name}", safe="/")
    try:
        github_empty(
            "DELETE",
            f"{GITHUB_API_BASE}/repos/{owner}/{repo}/git/refs/{encoded_ref}",
            token,
        )
    except RuntimeError as exc:
        if "HTTP 404" not in str(exc):
            raise
        print(f"Git tag ref already missing, skipping: {tag_name}")


def upload_github_release_asset(
    owner: str,
    repo: str,
    token: str,
    release_id: int,
    asset_path: Path,
) -> None:
    """Upload an asset to a GitHub release.

    @param owner Repository owner.
    @param repo Repository name.
    @param token GitHub token.
    @param release_id GitHub release ID.
    @param asset_path Local asset path.
    @returns None
    """

    query = parse.urlencode({"name": asset_path.name})
    url = f"{GITHUB_UPLOADS_BASE}/repos/{owner}/{repo}/releases/{release_id}/assets?{query}"
    headers = github_headers(token)
    headers["Content-Type"] = "application/octet-stream"
    req = request.Request(
        url,
        data=asset_path.read_bytes(),
        headers=headers,
        method="POST",
    )
    try:
        with request.urlopen(req) as response:
            response.read()
    except error.HTTPError as exc:
        payload = exc.read().decode("utf-8", errors="replace")
        raise RuntimeError(f"HTTP {exc.code} {url}: {payload}") from exc


def ensure_labels(owner: str, repo: str, token: str, labels: list[str]) -> None:
    """Ensure all issue labels exist.

    @param owner Repository owner.
    @param repo Repository name.
    @param token GitHub token.
    @param labels Label names to ensure.
    @returns None
    """

    color_map = {
        "release-detection": "0E8A16",
        "automated": "1D76DB",
    }
    for label in labels:
        try:
            http_json(
                "POST",
                f"{GITHUB_API_BASE}/repos/{owner}/{repo}/labels",
                headers=github_headers(token),
                body={
                    "name": label,
                    "color": color_map.get(label, "5319E7"),
                    "description": "Managed by the release detection workflow.",
                },
            )
        except RuntimeError as exc:
            if "already_exists" not in str(exc):
                raise


def list_tracking_issues(
    owner: str, repo: str, token: str, label: str
) -> list[dict[str, object]]:
    """List open tracking issues.

    @param owner Repository owner.
    @param repo Repository name.
    @param token GitHub token.
    @param label Tracking label.
    @returns Open issues with the configured label.
    """

    query = parse.urlencode({"state": "open", "labels": label, "per_page": 100})
    response = http_json(
        "GET",
        f"{GITHUB_API_BASE}/repos/{owner}/{repo}/issues?{query}",
        headers=github_headers(token),
    )
    if not isinstance(response, list):
        raise ValueError("GitHub issues API returned an unexpected payload.")
    return [issue for issue in response if isinstance(issue, dict)]


def find_tracking_issue(
    issues: list[dict[str, object]], title: str
) -> dict[str, object] | None:
    """Find an issue by exact title.

    @param issues Candidate issues.
    @param title Expected issue title.
    @returns Matching issue payload, if any.
    """

    for issue in issues:
        if issue.get("title") == title:
            return issue
    return None


def create_issue(
    owner: str,
    repo: str,
    token: str,
    title: str,
    body: str,
    labels: list[str],
) -> dict[str, object]:
    """Create a new tracking issue.

    @param owner Repository owner.
    @param repo Repository name.
    @param token GitHub token.
    @param title Issue title.
    @param body Issue body.
    @param labels Labels to assign.
    @returns Created issue payload.
    """

    response = http_json(
        "POST",
        f"{GITHUB_API_BASE}/repos/{owner}/{repo}/issues",
        headers=github_headers(token),
        body={"title": title, "body": body, "labels": labels},
    )
    return response


def update_issue(
    owner: str, repo: str, token: str, issue_number: int, body: str
) -> None:
    """Update an existing issue body.

    @param owner Repository owner.
    @param repo Repository name.
    @param token GitHub token.
    @param issue_number Issue number.
    @param body Updated issue body.
    @returns None
    """

    http_json(
        "PATCH",
        f"{GITHUB_API_BASE}/repos/{owner}/{repo}/issues/{issue_number}",
        headers=github_headers(token),
        body={"body": body},
    )


def create_issue_comment(
    owner: str, repo: str, token: str, issue_number: int, body: str
) -> None:
    """Create a comment on an issue.

    @param owner Repository owner.
    @param repo Repository name.
    @param token GitHub token.
    @param issue_number Issue number.
    @param body Comment body.
    @returns None
    """

    http_json(
        "POST",
        f"{GITHUB_API_BASE}/repos/{owner}/{repo}/issues/{issue_number}/comments",
        headers=github_headers(token),
        body={"body": body},
    )


def diff_channels(
    previous_state: dict[str, object] | None, snapshot: TargetSnapshot
) -> list[tuple[str, str | None, str]]:
    """Compare persisted state with the latest snapshot.

    @param previous_state Hidden state loaded from the issue body.
    @param snapshot Latest detected snapshot.
    @returns Changed channels with previous and new versions.
    """

    previous_channels: dict[str, object] = {}
    if previous_state:
        raw_channels = previous_state.get("channels")
        if isinstance(raw_channels, dict):
            previous_channels = raw_channels

    changes: list[tuple[str, str | None, str]] = []
    for channel, release in sorted(snapshot.channels.items()):
        previous_version = None
        previous_channel = previous_channels.get(channel)
        if isinstance(previous_channel, dict):
            raw_version = previous_channel.get("version")
            if isinstance(raw_version, str):
                previous_version = raw_version
        if previous_version != release.version:
            changes.append((channel, previous_version, release.version))
    return changes


def ensure_tracking_issue(
    owner: str,
    repo: str,
    token: str,
    target: dict[str, object],
    snapshot: TargetSnapshot,
    dry_run: bool,
) -> list[tuple[str, str | None, str]]:
    """Create or update the target tracking issue.

    @param owner Repository owner.
    @param repo Repository name.
    @param token GitHub token.
    @param target Target config entry.
    @param snapshot Latest detected snapshot.
    @param dry_run When true, print actions without mutating GitHub.
    @returns Changed channels with previous and current versions.
    """

    notify = target.get("notify")
    if not isinstance(notify, dict):
        raise ValueError(f"Target '{snapshot.target_id}' is missing a 'notify' object.")

    title = str(notify["issueTitle"])
    labels = [str(label) for label in notify.get("labels", ["release-detection"])]
    primary_label = labels[0]
    body = format_issue_body(snapshot)

    if dry_run:
        print(f"[dry-run] would ensure labels: {', '.join(labels)}")
        print(f"[dry-run] would upsert tracking issue: {title}")
        return []

    ensure_labels(owner, repo, token, labels)
    issues = list_tracking_issues(owner, repo, token, primary_label)
    issue = find_tracking_issue(issues, title)
    if issue is None:
        created = create_issue(owner, repo, token, title, body, labels)
        issue_number = created.get("number")
        print(f"Created tracking issue #{issue_number} for {snapshot.target_id}.")
        return diff_channels(None, snapshot)

    raw_number = issue.get("number")
    if not isinstance(raw_number, int):
        raise ValueError(f"Issue '{title}' is missing a numeric issue number.")
    issue_number = raw_number
    previous_state = extract_issue_state(str(issue.get("body", "")))
    changes = diff_channels(previous_state, snapshot)
    if not changes:
        print(f"No new version for {snapshot.target_id}.")
        return []

    update_issue(owner, repo, token, issue_number, body)
    comment = format_change_comment(snapshot, changes)
    create_issue_comment(owner, repo, token, issue_number, comment)
    print(f"Posted update comment to issue #{issue_number} for {snapshot.target_id}.")
    return changes


def publish_microsoft_store_release(
    owner: str,
    repo: str,
    token: str,
    target: dict[str, object],
    snapshot: TargetSnapshot,
    changes: list[tuple[str, str | None, str]],
    dry_run: bool,
) -> None:
    """Publish a Microsoft Store MSIX to a versioned GitHub release.

    @param owner Repository owner.
    @param repo Repository name.
    @param token GitHub token.
    @param target Target config entry.
    @param snapshot Latest detected snapshot.
    @param changes Changed channels from issue state comparison.
    @param dry_run When true, print actions without mutating GitHub.
    @returns None
    """

    release_config = target.get("release")
    if not isinstance(release_config, dict) or not release_config.get("enabled"):
        return

    source = target.get("source")
    if not isinstance(source, dict) or source.get("type") != "microsoft_store_web":
        raise ValueError(
            f"Target '{snapshot.target_id}' has release publishing enabled for an unsupported source type."
        )

    channel = str(release_config.get("channel", "stable"))
    release = snapshot.channels.get(channel)
    if release is None:
        raise ValueError(
            f"Target '{snapshot.target_id}' did not expose configured release channel '{channel}'."
        )

    tag_prefix = str(release_config.get("tagPrefix", "msstore-codex-v"))
    tag_name = f"{tag_prefix}{release.version}"
    release_name = str(
        release_config.get("nameTemplate", "Microsoft Store Codex {version}")
    ).format(version=release.version)
    expected_asset_name = f"OpenAI.Codex_{release.version}_x64__2p2nqsd0c76g0.msix"

    if dry_run:
        print(
            f"[dry-run] would evaluate Microsoft Store MSIX release publish for {snapshot.target_id}: tag={tag_name}"
        )
        return

    changed_channels = {changed_channel for changed_channel, _, _ in changes}
    existing_release = github_release_by_tag(owner, repo, token, tag_name)
    github_release = existing_release
    assets: list[dict[str, object]] = []
    if github_release is not None:
        print(f"GitHub release {tag_name} already exists.")
        raw_existing_release_id = github_release.get("id")
        if not isinstance(raw_existing_release_id, int):
            raise ValueError(f"GitHub release '{tag_name}' is missing a numeric id.")
        assets = list_github_release_assets(owner, repo, token, raw_existing_release_id)
        if any(asset.get("name") == expected_asset_name for asset in assets):
            print(f"Release asset already exists, skipping upload: {expected_asset_name}")
            return

    if channel not in changed_channels and github_release is None:
        print(
            f"GitHub release {tag_name} is missing; backfilling current {snapshot.target_id} MSIX."
        )

    if channel not in changed_channels and github_release is not None:
        print(
            f"Release asset missing for {tag_name}; backfilling current {snapshot.target_id} MSIX."
        )

    msix_path, download, _, _ = download_and_verify_microsoft_store_msix(
        target, release.version
    )
    if msix_path.name != download.filename:
        raise ValueError(
            f"Downloaded file name mismatch: expected {download.filename}, got {msix_path.name}"
        )

    if any(asset.get("name") == msix_path.name for asset in assets):
        print(f"Release asset already exists, skipping upload: {msix_path.name}")
        return

    if github_release is None:
        github_release = create_github_release(owner, repo, token, tag_name, release_name)
        print(f"Created GitHub release {tag_name}.")

    raw_release_id = github_release.get("id")
    if not isinstance(raw_release_id, int):
        raise ValueError(f"GitHub release '{tag_name}' is missing a numeric id.")

    upload_github_release_asset(owner, repo, token, raw_release_id, msix_path)
    print(f"Uploaded release asset: {msix_path.name}")


def cleanup_old_microsoft_store_releases(
    owner: str,
    repo: str,
    token: str,
    target: dict[str, object],
    dry_run: bool,
) -> None:
    """Delete versioned Microsoft Store releases outside the retention window.

    @param owner Repository owner.
    @param repo Repository name.
    @param token GitHub token.
    @param target Target config entry.
    @param dry_run When true, print actions without mutating GitHub.
    @returns None
    """

    release_config = target.get("release")
    if not isinstance(release_config, dict) or not release_config.get("enabled"):
        return

    source = target.get("source")
    if not isinstance(source, dict) or source.get("type") != "microsoft_store_web":
        return

    tag_prefix = str(release_config.get("tagPrefix", "msstore-codex-v"))
    retention_days = int(release_config.get("retentionDays", 30))
    cutoff = datetime.now(timezone.utc) - timedelta(days=retention_days)

    if dry_run:
        print(
            f"[dry-run] would delete Microsoft Store releases tagged with '{tag_prefix}' older than {retention_days} days."
        )
        return

    for github_release in list_github_releases(owner, repo, token):
        tag_name = github_release.get("tag_name")
        if not isinstance(tag_name, str) or not tag_name.startswith(tag_prefix):
            continue

        timestamp_value = github_release.get("published_at") or github_release.get(
            "created_at"
        )
        if not isinstance(timestamp_value, str) or not timestamp_value:
            print(f"Skipping release with no timestamp: {tag_name}")
            continue

        release_timestamp = parse_iso8601(timestamp_value)
        if release_timestamp >= cutoff:
            continue

        release_id = github_release.get("id")
        if not isinstance(release_id, int):
            raise ValueError(f"GitHub release '{tag_name}' is missing a numeric id.")

        delete_github_release(owner, repo, token, release_id)
        delete_github_tag_ref(owner, repo, token, tag_name)
        print(
            f"Deleted old Microsoft Store release and tag: {tag_name} ({to_iso8601(release_timestamp)})"
        )


def detect_snapshot(target: dict[str, object], token: str = "") -> TargetSnapshot:
    """Dispatch to the correct source handler.

    @param target Target config entry.
    @param token Optional GitHub token for authenticated upstream API requests.
    @returns Latest detected snapshot.
    """

    source = target.get("source")
    if not isinstance(source, dict):
        raise ValueError(f"Target '{target.get('id')}' is missing a 'source' object.")

    source_type = str(source.get("type"))
    if source_type == "vs_code_marketplace":
        return query_vs_code_marketplace(target)
    if source_type == "microsoft_store_web":
        return query_microsoft_store_web(target)
    if source_type == "github_releases":
        return query_github_releases(target, token)
    raise ValueError(f"Unsupported source type: {source_type}")


def main() -> int:
    """Run the release detection job.

    @param None
    @returns Process exit code.
    """

    args = parse_args()
    targets = load_config(args.config)

    token = os.environ.get("GITHUB_TOKEN", "")
    repository = os.environ.get("GITHUB_REPOSITORY", "")
    if not args.dry_run and (not token or "/" not in repository):
        raise ValueError(
            "GITHUB_TOKEN and GITHUB_REPOSITORY are required unless --dry-run is used."
        )

    owner = ""
    repo = ""
    if repository:
        owner, repo = repository.split("/", 1)

    for target in targets:
        snapshot = detect_snapshot(target, token)
        print(f"Detected {snapshot.target_id}:")
        for channel, release in sorted(snapshot.channels.items()):
            print(
                f"  - {channel}: version={release.version}, last_updated={release.last_updated}"
            )
        changes = ensure_tracking_issue(owner, repo, token, target, snapshot, args.dry_run)
        publish_microsoft_store_release(
            owner,
            repo,
            token,
            target,
            snapshot,
            changes,
            args.dry_run,
        )
        cleanup_old_microsoft_store_releases(
            owner,
            repo,
            token,
            target,
            args.dry_run,
        )

    return 0


if __name__ == "__main__":
    try:
        raise SystemExit(main())
    except Exception as exc:  # noqa: BLE001
        print(f"release_detection failed: {exc}", file=sys.stderr)
        raise SystemExit(1) from exc

#!/usr/bin/env python3
"""Scheduled release detection with GitHub issue notifications."""

from __future__ import annotations

import argparse
import json
import os
import sys
from dataclasses import dataclass
from datetime import datetime, timezone
from pathlib import Path
from typing import Final
from urllib import error, parse, request


MARKER_START: Final[str] = "<!-- release-detection-state"
MARKER_END: Final[str] = "-->"
DEFAULT_CONFIG_PATH: Final[Path] = Path("targets.json")
GITHUB_API_BASE: Final[str] = "https://api.github.com"
MARKETPLACE_API_URL: Final[str] = (
    "https://marketplace.visualstudio.com/_apis/public/gallery/extensionquery"
)
MARKETPLACE_ACCEPT: Final[str] = "application/json;api-version=7.2-preview.1"


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


def github_headers(token: str) -> dict[str, str]:
    """Build GitHub API headers.

    @param token GitHub token.
    @returns HTTP headers for the GitHub REST API.
    """

    return {
        "Accept": "application/vnd.github+json",
        "Authorization": f"Bearer {token}",
        "X-GitHub-Api-Version": "2022-11-28",
    }


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
) -> None:
    """Create or update the target tracking issue.

    @param owner Repository owner.
    @param repo Repository name.
    @param token GitHub token.
    @param target Target config entry.
    @param snapshot Latest detected snapshot.
    @param dry_run When true, print actions without mutating GitHub.
    @returns None
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
        return

    ensure_labels(owner, repo, token, labels)
    issues = list_tracking_issues(owner, repo, token, primary_label)
    issue = find_tracking_issue(issues, title)
    if issue is None:
        created = create_issue(owner, repo, token, title, body, labels)
        issue_number = created.get("number")
        print(f"Created tracking issue #{issue_number} for {snapshot.target_id}.")
        return

    raw_number = issue.get("number")
    if not isinstance(raw_number, int):
        raise ValueError(f"Issue '{title}' is missing a numeric issue number.")
    issue_number = raw_number
    previous_state = extract_issue_state(str(issue.get("body", "")))
    changes = diff_channels(previous_state, snapshot)
    if not changes:
        print(f"No new version for {snapshot.target_id}.")
        return

    update_issue(owner, repo, token, issue_number, body)
    comment = format_change_comment(snapshot, changes)
    create_issue_comment(owner, repo, token, issue_number, comment)
    print(f"Posted update comment to issue #{issue_number} for {snapshot.target_id}.")


def detect_snapshot(target: dict[str, object]) -> TargetSnapshot:
    """Dispatch to the correct source handler.

    @param target Target config entry.
    @returns Latest detected snapshot.
    """

    source = target.get("source")
    if not isinstance(source, dict):
        raise ValueError(f"Target '{target.get('id')}' is missing a 'source' object.")

    source_type = str(source.get("type"))
    if source_type == "vs_code_marketplace":
        return query_vs_code_marketplace(target)
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
        snapshot = detect_snapshot(target)
        print(f"Detected {snapshot.target_id}:")
        for channel, release in sorted(snapshot.channels.items()):
            print(
                f"  - {channel}: version={release.version}, last_updated={release.last_updated}"
            )
        ensure_tracking_issue(owner, repo, token, target, snapshot, args.dry_run)

    return 0


if __name__ == "__main__":
    try:
        raise SystemExit(main())
    except Exception as exc:  # noqa: BLE001
        print(f"release_detection failed: {exc}", file=sys.stderr)
        raise SystemExit(1) from exc

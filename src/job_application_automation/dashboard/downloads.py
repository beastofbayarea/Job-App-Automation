"""Contained file resolution for public artifact and admin-vault downloads."""

from __future__ import annotations

import mimetypes
from dataclasses import dataclass
from pathlib import Path
from typing import TypeAlias
from collections.abc import Callable

from .artifacts import get_output_file_path
from .models import DashboardContext, DashboardRequest, HttpResponse


@dataclass(frozen=True, slots=True)
class DownloadTarget:
    """A contained regular file and its public response metadata."""

    path: Path
    content_type: str
    disposition: str


DownloadResolution: TypeAlias = DownloadTarget | HttpResponse


def _contained_regular_file(root: Path, candidate: Path) -> Path | None:
    """Resolve a non-symlink regular file only when it remains below ``root``."""

    try:
        if candidate.is_symlink():
            return None
        resolved_root = root.resolve()
        resolved = candidate.resolve()
        resolved.relative_to(resolved_root)
        if not resolved.is_file():
            return None
    except (OSError, ValueError):
        return None
    return resolved


def resolve_public_download(
    context: DashboardContext,
    request: DashboardRequest,
) -> DownloadResolution:
    """Resolve one allowlisted raw artifact beneath the output root."""

    prefix = "/api/download/"
    suffix = request.path[len(prefix) :] if request.path.startswith(prefix) else ""
    filename = Path(suffix).name
    if not suffix or suffix != filename or "\\" in suffix or filename in {".", ".."}:
        return HttpResponse.json({"error": "Invalid filename"}, status=400)
    if filename not in context.public_raw_files:
        return HttpResponse.json(
            {"error": "This raw artifact is available through the Admin Vault"},
            status=403,
        )

    output_root = context.paths.output_dir
    candidates = [output_root / filename, output_root / "vps_reports" / filename]
    if not any(candidate.exists() for candidate in candidates) and output_root.is_dir():
        try:
            for path in output_root.rglob(filename):
                candidates.append(path)
                break
        except OSError:
            pass

    target = next(
        (
            resolved
            for candidate in candidates
            if (resolved := _contained_regular_file(output_root, candidate)) is not None
        ),
        None,
    )
    if target is None:
        return HttpResponse.json({"error": f"File not found: {filename}"}, status=404)
    return DownloadTarget(
        path=target,
        content_type="application/pdf"
        if filename.lower().endswith(".pdf")
        else "application/octet-stream",
        disposition=f'attachment; filename="{filename}"',
    )


def resolve_admin_download(
    context: DashboardContext,
    request: DashboardRequest,
    *,
    is_repository_file_displayable: Callable[[Path], bool],
) -> DownloadResolution:
    """Resolve a query-selected vault file within one declared scope."""

    scope = request.first_query_value("scope")
    relative = request.first_query_value("path")
    roots = {
        "output": context.paths.output_dir,
        "private_archive": context.paths.private_archive_dir,
        "repository": context.paths.project_root,
    }
    root = roots.get(scope)
    if root is None or not relative:
        return HttpResponse.json({"error": "scope and path are required"}, status=400)

    requested = root / relative
    target = _contained_regular_file(root, requested)
    if target is None:
        try:
            resolved_root = root.resolve()
            resolved_requested = requested.resolve()
            resolved_requested.relative_to(resolved_root)
        except (OSError, ValueError):
            return HttpResponse.json({"error": "Invalid file path"}, status=400)
        return HttpResponse.json({"error": "File not found"}, status=404)
    if scope == "repository" and not is_repository_file_displayable(target):
        return HttpResponse.json({"error": "File is not displayable"}, status=403)

    safe_name = target.name.replace('"', "_").replace("\r", "_").replace("\n", "_")
    return DownloadTarget(
        path=target,
        content_type=mimetypes.guess_type(target.name)[0] or "application/octet-stream",
        disposition=f'inline; filename="{safe_name}"',
    )


def render_download(resolution: DownloadResolution) -> HttpResponse:
    """Read a resolved target into a typed HTTP response."""

    if isinstance(resolution, HttpResponse):
        return resolution
    try:
        data = resolution.path.read_bytes()
    except OSError as exc:
        return HttpResponse.json({"error": str(exc)}, status=500)
    return HttpResponse.binary(
        data,
        content_type=resolution.content_type,
        disposition=resolution.disposition,
    )


def public_text_file_response(
    context: DashboardContext,
    request: DashboardRequest,
) -> HttpResponse:
    """Return one allowlisted text artifact in the historical JSON envelope."""

    prefix = "/api/files/"
    suffix = request.path[len(prefix) :] if request.path.startswith(prefix) else ""
    filename = Path(suffix).name
    if not suffix or suffix != filename or "\\" in suffix:
        return HttpResponse.json(
            {"error": "This raw artifact is available through the Admin Vault"},
            status=403,
        )
    if filename not in context.public_raw_files:
        return HttpResponse.json(
            {"error": "This raw artifact is available through the Admin Vault"},
            status=403,
        )
    file_path = get_output_file_path(context, filename)
    target = _contained_regular_file(context.paths.output_dir, file_path)
    if target is None:
        return HttpResponse.json({"error": "File not found"}, status=404)
    try:
        content = target.read_text(encoding="utf-8", errors="replace")
    except OSError as exc:
        return HttpResponse.json({"error": str(exc)}, status=500)
    return HttpResponse.json({"filename": filename, "path": str(target), "content": content})

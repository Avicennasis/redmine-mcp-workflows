"""Project files tools (Redmine ticket #4494).

Tools (2):
  - list_project_files  — GET  /projects/{id}/files.json
  - upload_project_file — POST /projects/{id}/files.json (via upload token)

Uses the same upload token flow as the existing attachment upload.
"""

from __future__ import annotations

from pathlib import Path
from typing import Any

from ..cache.schema_db import SchemaCache
from ..client import RedmineClient
from ..errors import RedmineAPIError


async def list_project_files(
    client: RedmineClient,
    cache: SchemaCache,  # noqa: ARG001
    *,
    project_id: int | str,
) -> dict[str, Any]:
    """List files on a project."""
    try:
        resp = await client.get(f"/projects/{project_id}/files.json")
    except RedmineAPIError as e:
        return e.as_structured()

    files = resp.get("files", []) if isinstance(resp, dict) else []
    return {
        "files": files,
        "count": len(files),
        "project_id": project_id,
        "source": "api",
    }


async def upload_project_file(
    client: RedmineClient,
    cache: SchemaCache,  # noqa: ARG001
    *,
    project_id: int | str,
    file_path: str,
    allowed_directories: list[str],
    filename: str | None = None,
    description: str | None = None,
    version_id: int | None = None,
) -> dict[str, Any]:
    """Upload a file to a project's Files section.

    Args:
        project_id: project id or slug.
        file_path: path to the file to upload (path-restricted).
        allowed_directories: list of allowed parent directories.
        filename: optional override for the file name.
        description: optional file description.
        version_id: optional version to associate the file with.
    """
    resolved = Path(file_path).expanduser().resolve()

    if not resolved.is_file():
        return {
            "error": "file_not_found",
            "hint": f"No regular file at {resolved}.",
            "path": str(resolved),
        }

    allowed = False
    for d in allowed_directories:
        try:
            resolved.relative_to(Path(d).expanduser().resolve())
            allowed = True
            break
        except ValueError:
            continue

    if not allowed:
        return {
            "error": "path_not_allowed",
            "hint": (f"Path {resolved} is not under any allowed directory: {allowed_directories}."),
            "path": str(resolved),
            "allowed_directories": allowed_directories,
        }

    data = resolved.read_bytes()

    try:
        upload_resp = await client.post_binary("/uploads.json", data=data)
    except RedmineAPIError as e:
        return e.as_structured()

    token = upload_resp.get("upload", {}).get("token") if isinstance(upload_resp, dict) else None
    if not token:
        return {
            "error": "upload_token_missing",
            "hint": "Redmine did not return an upload token.",
        }

    file_body: dict[str, Any] = {"token": token}
    safe_name = Path(filename).name if filename else resolved.name
    if not safe_name:
        safe_name = resolved.name
    file_body["filename"] = safe_name
    if description:
        file_body["description"] = description
    if version_id is not None:
        file_body["version_id"] = version_id

    try:
        await client.post(
            f"/projects/{project_id}/files.json",
            json={"file": file_body},
        )
    except RedmineAPIError as e:
        return e.as_structured()

    return {
        "uploaded": True,
        "filename": file_body["filename"],
        "project_id": project_id,
        "source": "api",
    }

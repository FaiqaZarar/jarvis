"""Listing Drive files and folders."""

from typing import Optional

from jarvis_server.connectors.drive._constants import DRIVE_API_NUM_RETRIES

_FOLDER_MIME = "application/vnd.google-apps.folder"


def list_drive_files(service, folder_id: Optional[str] = None) -> list[dict]:
    """Non-trashed files as ``{id, name, mimeType, modifiedTime,
    shortcutDetails, driveId}`` dicts (last three omitted where Drive has
    nothing to report). Scoped to My Drive + Shared-with-me only - the
    ``supportsAllDrives``/``includeItemsFromAllDrives`` flags stay unset.
    ``driveId`` is requested so `process_drive_file` can set the
    shared-drive flag.

    `folder_id` restricts to direct children of one folder; without it,
    every file the account can see is returned, folders included.
    """
    query_parts = ["trashed = false"]
    if folder_id:
        query_parts.append(f"'{folder_id}' in parents")
    query = " and ".join(query_parts)

    files: list[dict] = []
    page_token: Optional[str] = None
    while True:
        response = (
            service.files()
            .list(
                q=query,
                spaces="drive",
                fields="nextPageToken, files(id, name, mimeType, modifiedTime, shortcutDetails, driveId)",
                pageSize=100,
                pageToken=page_token,
            )
            .execute(num_retries=DRIVE_API_NUM_RETRIES)
        )
        files.extend(response.get("files", []))
        page_token = response.get("nextPageToken")
        if not page_token:
            break
    return files


def list_drive_folders(service, parent_id: str = "root") -> list[dict]:
    """Direct sub-folders of ``parent_id`` (``"root"`` = My Drive's top
    level), as ``[{"id", "name"}, ...]`` ordered by name.

    Powers the folder-picker CLI. Kept separate from ``list_drive_files``,
    which lists every file type and needs more fields.
    """
    query = (
        f"'{parent_id}' in parents "
        f"and mimeType = '{_FOLDER_MIME}' "
        "and trashed = false"
    )

    folders: list[dict] = []
    page_token: Optional[str] = None
    while True:
        response = (
            service.files()
            .list(
                q=query,
                spaces="drive",
                fields="nextPageToken, files(id, name)",
                orderBy="name",
                pageSize=100,
                pageToken=page_token,
            )
            .execute(num_retries=DRIVE_API_NUM_RETRIES)
        )
        folders.extend(
            {"id": f["id"], "name": f["name"]} for f in response.get("files", [])
        )
        page_token = response.get("nextPageToken")
        if not page_token:
            break
    return folders


def list_drive_files_recursive(service, folder_id: str) -> list[dict]:
    """Every non-folder file under ``folder_id`` at any depth, deduped by id
    (a file can have several parents) - the Drive equivalent of
    ``os.walk(followlinks=False)``: a shortcut is returned as a file,
    never followed as a directory, since its target can live anywhere in
    the Drive and descending into it would undo the ``folder_id`` scoping.

    ``visited`` guards both a folder reachable by two paths and a parent
    cycle that would otherwise recurse forever.
    """
    files_by_id: dict[str, dict] = {}
    visited: set[str] = set()
    queue: list[str] = [folder_id]

    while queue:
        current = queue.pop(0)
        if current in visited:
            continue
        visited.add(current)

        for entry in list_drive_files(service, folder_id=current):
            if entry.get("mimeType") == _FOLDER_MIME:
                if entry["id"] not in visited:
                    queue.append(entry["id"])
            else:
                files_by_id.setdefault(entry["id"], entry)

    return list(files_by_id.values())

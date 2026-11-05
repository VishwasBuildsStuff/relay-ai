"""Portable repository path normalization shared by ingress and workspace writers."""

from __future__ import annotations

import re
import unicodedata
from pathlib import PurePosixPath
from collections.abc import Mapping


_DRIVE_PREFIX = re.compile(r"^[A-Za-z]:")
_WINDOWS_RESERVED = {"CON", "PRN", "AUX", "NUL", *(f"COM{i}" for i in range(1, 10)), *(f"LPT{i}" for i in range(1, 10))}


def normalize_relative_path(raw_path: str) -> tuple[str, str]:
    """Return an NFC relative path and a portable case-insensitive collision key."""
    if not isinstance(raw_path, str):
        raise ValueError("Repository paths must be strings.")
    normalized = unicodedata.normalize("NFC", raw_path.replace("\\", "/"))
    path = PurePosixPath(normalized)
    parts = normalized.split("/")
    if (
        not normalized
        or len(normalized) > 512
        or len(parts) > 32
        or path.is_absolute()
        or _DRIVE_PREFIX.match(normalized)
        or any(part in ("", ".", "..") for part in parts)
        or any(ord(char) < 32 or ord(char) == 127 or char == ":" for char in normalized)
    ):
        raise ValueError(f"Unsafe repository path: {raw_path!r}")
    for part in parts:
        try:
            component_bytes = len(part.encode("utf-8"))
        except UnicodeEncodeError as error:
            raise ValueError(f"Repository path is not valid UTF-8: {raw_path!r}") from error
        if part.endswith((" ", ".")) or component_bytes > 255:
            raise ValueError(f"Repository path component is not portable: {raw_path!r}")
        if part.split(".", 1)[0].upper() in _WINDOWS_RESERVED:
            raise ValueError(f"Repository path uses a reserved filename: {raw_path!r}")
        if part.casefold() == ".git":
            raise ValueError("Repository snapshots may not contain a .git directory.")
    return normalized, normalized.casefold()


def normalize_snapshot_paths(codebase: Mapping[str, str]) -> dict[str, str]:
    """Normalize a file map and reject case/Unicode aliases and file/directory collisions."""
    normalized_files: dict[str, str] = {}
    identities: set[str] = set()
    for raw_path, content in codebase.items():
        normalized_path, identity = normalize_relative_path(raw_path)
        try:
            content.encode("utf-8")
        except (AttributeError, UnicodeEncodeError) as error:
            raise ValueError(f"Repository file contents must be UTF-8 text: {raw_path!r}") from error
        if identity in identities:
            raise ValueError(f"Repository snapshot contains duplicate normalized paths near {raw_path!r}.")
        parts = identity.split("/")
        parents = {"/".join(parts[:index]) for index in range(1, len(parts))}
        if parents & identities or any(existing.startswith(identity + "/") for existing in identities):
            raise ValueError(f"Repository snapshot has a file/directory path collision at {raw_path!r}.")
        identities.add(identity)
        normalized_files[normalized_path] = content
    return normalized_files

"""Structured error type shared by the core, the CLI and the MCP server."""

from __future__ import annotations

from typing import Any, Dict, Optional

# Stable, machine-readable error codes. Keep them ASCII and lowercase.
CODES = (
    "invalid_arguments",
    "project_not_found",
    "project_is_file",
    "no_project_index",
    "multiple_project_index",
    "project_index_unreadable",
    "file_not_found",
    "file_is_directory",
    "absolute_path_rejected",
    "path_traversal_rejected",
    "path_outside_project",
    "symlink_rejected",
    "unsupported_format",
    "unknown_il_header",
    "unsupported_il_payload",
    "unsupported_pou_type",
    "hash_mismatch",
    "old_text_not_found",
    "old_text_ambiguous",
    "invalid_line_endings",
    "payload_too_large",
    "destination_exists",
    "destination_inside_source",
    "source_inside_destination",
    "destination_not_writable",
    "probe_unavailable",
    "internal_error",
)


class ToolError(Exception):
    """An error that is reported as a structured JSON object, never a traceback."""

    def __init__(self, code: str, message: str, details: Optional[Dict[str, Any]] = None):
        super().__init__(message)
        self.code = code
        self.message = message
        self.details: Dict[str, Any] = dict(details or {})

    def to_dict(self) -> Dict[str, Any]:
        error: Dict[str, Any] = {"code": self.code, "message": self.message}
        if self.details:
            error["details"] = self.details
        return {"ok": False, "error": error}

    def __str__(self) -> str:  # pragma: no cover - trivial
        return "%s: %s" % (self.code, self.message)

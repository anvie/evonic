"""Shared native-document classification and model capability helpers."""

from __future__ import annotations

import os
from typing import Optional


PDF = "pdf"
TEXT = "text"
OFFICE = "office"
SPREADSHEET = "spreadsheet"

DOCUMENT_CAPABILITIES = {
    PDF: "document_pdf_supported",
    OFFICE: "document_office_supported",
    SPREADSHEET: "document_spreadsheet_supported",
}

TEXT_EXTENSIONS = frozenset({
    ".txt", ".md", ".markdown", ".log", ".json", ".yaml", ".yml",
    ".xml", ".py", ".js", ".ts", ".tsx", ".jsx", ".java", ".go",
    ".rs", ".c", ".cc", ".cpp", ".h", ".hpp", ".rb", ".php",
    ".kt", ".swift", ".html", ".htm", ".css", ".scss", ".sql",
    ".sh", ".toml", ".ini", ".cfg", ".conf", ".env", ".rst",
    ".tex",
})

OFFICE_EXTENSIONS = frozenset({
    ".doc", ".docx", ".dot", ".odt", ".rtf",
    ".ppt", ".pptx", ".pps", ".pot", ".ppa", ".pwz", ".wiz",
    ".pages",
})

SPREADSHEET_EXTENSIONS = frozenset({
    ".csv", ".tsv", ".iif", ".xla", ".xlb", ".xlc", ".xlm",
    ".xls", ".xlsx", ".xlt", ".xlw",
})

DOCUMENT_EXTENSIONS = {
    ".pdf": PDF,
    **{ext: TEXT for ext in TEXT_EXTENSIONS},
    **{ext: OFFICE for ext in OFFICE_EXTENSIONS},
    **{ext: SPREADSHEET for ext in SPREADSHEET_EXTENSIONS},
}
SUPPORTED_DOCUMENT_EXTENSIONS = frozenset(DOCUMENT_EXTENSIONS)

_MIME_CATEGORIES = {
    "application/pdf": PDF,
    "application/json": TEXT,
    "application/xml": TEXT,
    "application/x-yaml": TEXT,
    "application/yaml": TEXT,
    "application/javascript": TEXT,
    "application/x-javascript": TEXT,
    "application/x-sh": TEXT,
    "application/sql": TEXT,
    "text/csv": SPREADSHEET,
    "application/csv": SPREADSHEET,
    "text/tab-separated-values": SPREADSHEET,
    "application/vnd.ms-excel": SPREADSHEET,
    "application/vnd.openxmlformats-officedocument.spreadsheetml.sheet": SPREADSHEET,
    "application/vnd.shana.informed.interchange": SPREADSHEET,
    "application/msword": OFFICE,
    "application/vnd.openxmlformats-officedocument.wordprocessingml.document": OFFICE,
    "application/vnd.oasis.opendocument.text": OFFICE,
    "application/rtf": OFFICE,
    "text/rtf": OFFICE,
    "application/vnd.ms-powerpoint": OFFICE,
    "application/vnd.openxmlformats-officedocument.presentationml.presentation": OFFICE,
    "application/vnd.apple.pages": OFFICE,
    "application/x-iwork-pages-sffpages": OFFICE,
}

_TEXTUAL_NON_TEXT_EXTENSIONS = frozenset({".csv", ".tsv", ".iif", ".rtf"})
_AMBIGUOUS_TEXT_EXTENSIONS = frozenset({".dot", ".pot"})
_GENERIC_MIMES = frozenset({"", "application/octet-stream", "binary/octet-stream"})


def document_extension(filename: str) -> str:
    name = os.path.basename(str(filename or "")).lower()
    ext = os.path.splitext(name)[1]
    return ext or (name if name in DOCUMENT_EXTENSIONS else "")


def document_category(filename: str, mime_type: Optional[str] = None) -> Optional[str]:
    """Classify a supported document, rejecting recognized MIME conflicts."""
    ext = document_extension(filename)
    ext_category = DOCUMENT_EXTENSIONS.get(ext)
    mime = str(mime_type or "").split(";", 1)[0].strip().lower()
    mime_category = _MIME_CATEGORIES.get(mime)
    if mime.startswith("text/") and mime_category is None:
        mime_category = TEXT

    if not ext_category:
        return None
    if mime in _GENERIC_MIMES:
        return ext_category
    if not mime_category:
        return None
    if mime_category == ext_category:
        return ext_category
    if mime_category == TEXT and ext in _AMBIGUOUS_TEXT_EXTENSIONS:
        return TEXT
    if mime_category == TEXT and ext in _TEXTUAL_NON_TEXT_EXTENSIONS:
        return ext_category
    return None


def is_text_document(filename: str, mime_type: Optional[str] = None) -> bool:
    category = document_category(filename, mime_type)
    return category == TEXT or document_extension(filename) in {".csv", ".tsv", ".iif"}


def capability_for(category: str) -> Optional[str]:
    return DOCUMENT_CAPABILITIES.get(category)


def model_supports_any_document(model: dict) -> bool:
    return any(model.get(column) for column in DOCUMENT_CAPABILITIES.values())


def analysis_guidance(filename: str, mime_type: Optional[str], path,
                      enabled: bool = True, attachment_id=None) -> str:
    category = document_category(filename, mime_type)
    if not category:
        return ""
    if is_text_document(filename, mime_type):
        if attachment_id is None:
            return ""
        return (
            f"Use `read_attachment` with attachment_id={attachment_id} to read "
            "this text-based file exactly."
        )
    if not enabled or not path:
        return ""
    return (
        f"Use `analyze_document` with path `{path}` to analyze the "
        f"{category} document natively."
    )

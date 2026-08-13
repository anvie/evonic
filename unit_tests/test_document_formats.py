"""Supported document format contract shared by uploads, hints, and tools."""

import io

import pytest
from werkzeug.datastructures import FileStorage

from backend.tools._document import (
    OFFICE,
    PDF,
    SPREADSHEET,
    TEXT,
    document_category,
)
from routes.sessions import _ALLOWED_EXTS, _process_upload


@pytest.mark.parametrize(('filename', 'mime_type', 'category'), [
    ('report.pdf', 'application/pdf', PDF),
    ('notes.md', 'text/markdown', TEXT),
    ('.env', 'text/plain', TEXT),
    ('code.py', 'text/x-python', TEXT),
    ('report.docx', 'application/vnd.openxmlformats-officedocument.wordprocessingml.document', OFFICE),
    ('slides.pptx', 'application/vnd.openxmlformats-officedocument.presentationml.presentation', OFFICE),
    ('table.csv', 'text/csv', SPREADSHEET),
    ('ledger.iif', 'application/vnd.shana.informed.interchange', SPREADSHEET),
    ('book.xlsx', 'application/vnd.openxmlformats-officedocument.spreadsheetml.sheet', SPREADSHEET),
])
def test_document_categories(filename, mime_type, category):
    assert document_category(filename, mime_type) == category


@pytest.mark.parametrize(('filename', 'mime_type'), [
    ('archive.zip', 'application/zip'),
    ('book.ods', 'application/vnd.oasis.opendocument.spreadsheet'),
    ('slides.odp', 'application/vnd.oasis.opendocument.presentation'),
    ('book.epub', 'application/epub+zip'),
    ('report.bin', 'application/pdf'),
    ('script.exe', 'text/plain'),
    ('fake.pdf', 'application/zip'),
    ('fake.txt', 'application/pdf'),
])
def test_unsupported_and_mismatched_documents_are_rejected(filename, mime_type):
    assert document_category(filename, mime_type) is None


def test_chat_allowlist_rejects_archives_and_unsupported_documents():
    assert {'.zip', '.ods', '.odp', '.epub'}.isdisjoint(_ALLOWED_EXTS)
    assert {'.pdf', '.txt', '.docx', '.pptx', '.xlsx'} <= _ALLOWED_EXTS


def test_upload_rejects_mime_mismatch_before_writing(tmp_path, monkeypatch):
    monkeypatch.chdir(tmp_path)
    upload = FileStorage(
        stream=io.BytesIO(b'PK archive bytes'),
        filename='fake.pdf',
        content_type='application/zip',
    )

    with pytest.raises(ValueError, match='MIME type does not match'):
        _process_upload(upload, 'agent', 'session', 'user', 'channel')

    assert not (tmp_path / 'data').exists()

import os
import re
import tempfile
import unittest
from io import BytesIO
from unittest import mock

from werkzeug.datastructures import FileStorage

from routes import sessions


def _ext(name):
    return os.path.splitext(name)[1].lower()


class AttachmentTypesTest(unittest.TestCase):
    def test_archives_plain_files_and_images_are_allowed(self):
        names = ['a.zip', 'a.gz', 'a.tar.gz', 'a.tgz', 'a.tar.bz2', 'a.tbz2', 'a.rar',
                 'a.txt', 'a.json', 'a.md', 'a.jsonl', 'a.ndjson',
                 'a.patch', 'a.PATCH', 'a.diff',
                 'a.png', 'a.JPG', 'a.bmp', 'a.tiff', 'a.svg', 'a.heic', 'a.avif']
        for name in names:
            self.assertIn(_ext(name), sessions._ALLOWED_EXTS, name)

    def test_executables_are_still_refused(self):
        for name in ['a.exe', 'a.dll', 'a.bat', 'a']:
            self.assertNotIn(_ext(name), sessions._ALLOWED_EXTS, name)

    def _upload(self, name, data, content_type):
        with tempfile.TemporaryDirectory() as tmp, mock.patch.object(sessions, 'db') as db:
            db.save_attachment.return_value = 1
            db.get_agent.return_value = None
            cwd = os.getcwd()
            os.chdir(tmp)
            try:
                return sessions._process_upload(
                    FileStorage(BytesIO(data), filename=name, content_type=content_type), 'a', 's', 'u', None)
            finally:
                os.chdir(cwd)

    def test_jsonl_is_read_inline(self):
        out = self._upload('rows.jsonl', b'{"a": 1}\n{"a": 2}\n', 'application/octet-stream')
        self.assertIn('{"a": 2}', out['text_prefix'])

    def test_patch_is_read_inline(self):
        patch = b'diff --git a/a.py b/a.py\n--- a/a.py\n+++ b/a.py\n@@ -1 +1 @@\n-x = 1\n+x = 2\n'
        out = self._upload('fix.patch', patch, 'application/octet-stream')
        self.assertIn('+x = 2', out['text_prefix'])
        self.assertFalse(out['attachment_info']['is_image'])

    def test_archive_is_stored_as_a_document(self):
        out = self._upload('x.tar.gz', b'\x1f\x8b' + b'0' * 10, 'application/gzip')
        self.assertFalse(out['attachment_info']['is_image'])
        self.assertIn('x.tar.gz', out['text_prefix'])

    def test_unrenderable_image_is_still_an_image_attachment(self):
        out = self._upload('pic.heic', b'not decodable', 'application/octet-stream')
        self.assertTrue(out['attachment_info']['is_image'])
        self.assertIsNone(out['image_url'])


class AttachmentAcceptAttributeTest(unittest.TestCase):
    """Web composers must offer the same patch formats the upload API accepts."""

    _REPO_ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
    _COMPOSERS = (
        ('templates/sessions.html', 'file-input'),
        ('templates/agent_detail.html', 'chat-file-input'),
    )

    def _accept_for(self, template, input_id):
        """Return the comma-separated `accept` tokens of the given file input."""
        with open(os.path.join(self._REPO_ROOT, template), encoding='utf-8') as fh:
            html = fh.read()
        tag = re.search(
            r'<input\b[^>]*\bid=["\']%s["\'][^>]*>' % re.escape(input_id), html
        )
        self.assertIsNotNone(tag, f'{input_id} not found in {template}')
        accept = re.search(r'accept="([^"]*)"', tag.group(0))
        self.assertIsNotNone(accept, f'{input_id} has no accept attribute in {template}')
        return {token.strip().lower() for token in accept.group(1).split(',')}

    def test_patch_and_diff_are_selectable_in_web_composers(self):
        for template, input_id in self._COMPOSERS:
            accept = self._accept_for(template, input_id)
            for ext in ('.patch', '.diff'):
                self.assertIn(ext, accept, f'{ext} not selectable in {template}')
                self.assertIn(ext, sessions._ALLOWED_EXTS,
                              f'{ext} not accepted by the upload API')

    def test_patch_mime_types_are_treated_as_text(self):
        from backend.tools.read_attachment import _is_textish

        for mime in ('text/x-patch', 'text/x-diff', 'application/x-patch',
                     'application/x-diff', 'application/octet-stream'):
            self.assertTrue(_is_textish(mime, 'fix.patch'), mime)


if __name__ == '__main__':
    unittest.main()

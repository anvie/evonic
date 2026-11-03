import os
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

    def test_archive_is_stored_as_a_document(self):
        out = self._upload('x.tar.gz', b'\x1f\x8b' + b'0' * 10, 'application/gzip')
        self.assertFalse(out['attachment_info']['is_image'])
        self.assertIn('x.tar.gz', out['text_prefix'])

    def test_unrenderable_image_is_still_an_image_attachment(self):
        out = self._upload('pic.heic', b'not decodable', 'application/octet-stream')
        self.assertTrue(out['attachment_info']['is_image'])
        self.assertIsNone(out['image_url'])


if __name__ == '__main__':
    unittest.main()

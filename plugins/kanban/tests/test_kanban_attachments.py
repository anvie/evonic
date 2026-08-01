"""
Tests for the kanban attachment upload/list/serve/delete API routes.

Each test uses an isolated temp database and a temp uploads directory so the
real kanban.db and real upload store are never touched.
"""

import io
import os
import shutil
import tempfile
import unittest
from unittest.mock import patch

from flask import Flask

from plugins.kanban import routes


def _make_client():
    app = Flask(__name__)
    app.register_blueprint(routes.create_blueprint())
    return app.test_client()


# Minimal valid 1x1 PNG.
_PNG_BYTES = bytes.fromhex(
    '89504e470d0a1a0a0000000d49484452000000010000000108060000001f15c489'
    '0000000d49444154789c626001000000ffff03000006000557bfabd40000000049454e44ae426082'
)


class TestKanbanAttachmentsAPI(unittest.TestCase):
    def setUp(self):
        self.temp_dir = tempfile.mkdtemp()
        from plugins.kanban.db import KanbanDB

        self.db = KanbanDB(db_path=os.path.join(self.temp_dir, 'kanban.db'))
        self.uploads_dir = os.path.join(self.temp_dir, 'uploads')
        os.makedirs(self.uploads_dir, exist_ok=True)

        self._db_patcher = patch('plugins.kanban.routes.kanban_db', self.db)
        self._dir_patcher = patch('plugins.kanban.routes.ATTACHMENTS_DIR', self.uploads_dir)
        self._db_patcher.start()
        self._dir_patcher.start()
        self.client = _make_client()

    def tearDown(self):
        self._db_patcher.stop()
        self._dir_patcher.stop()
        shutil.rmtree(self.temp_dir, ignore_errors=True)

    # ---- helpers ----

    def _create_task(self):
        resp = self.client.post('/api/kanban/tasks', json={
            'title': 'Attachment task',
            'description': 'has attachments',
        })
        self.assertEqual(resp.status_code, 201)
        return resp.get_json()['task']

    def _upload(self, task_id, files):
        return self.client.post(
            f'/api/kanban/tasks/{task_id}/attachments',
            data=files,
            content_type='multipart/form-data',
        )

    # ---- create / list enrichment ----

    def test_task_created_with_empty_attachments(self):
        task = self._create_task()
        self.assertEqual(task.get('attachments'), [])

    def test_tasks_list_includes_attachments(self):
        self._create_task()
        resp = self.client.get('/api/kanban/tasks')
        self.assertEqual(resp.status_code, 200)
        tasks = resp.get_json()['tasks']
        self.assertTrue(tasks)
        self.assertTrue(all('attachments' in t for t in tasks))

    # ---- upload validation ----

    def test_upload_valid_image(self):
        task = self._create_task()
        resp = self._upload(task['id'], {'files': (io.BytesIO(_PNG_BYTES), 'photo.png')})
        self.assertEqual(resp.status_code, 201)
        data = resp.get_json()
        self.assertEqual(len(data['attachments']), 1)
        att = data['attachments'][0]
        self.assertEqual(att['filename'], 'photo.png')
        self.assertEqual(att['mime_type'], 'image/png')
        self.assertTrue(att['url'].endswith('/file'))

    def test_upload_multiple_images(self):
        task = self._create_task()
        resp = self._upload(task['id'], {
            'files': [
                (io.BytesIO(_PNG_BYTES), 'a.png'),
                (io.BytesIO(_PNG_BYTES), 'b.png'),
            ],
        })
        self.assertEqual(resp.status_code, 201)
        self.assertEqual(len(resp.get_json()['attachments']), 2)

    def test_upload_rejects_non_image(self):
        task = self._create_task()
        resp = self._upload(task['id'], {'files': (io.BytesIO(b'hello world'), 'note.txt')})
        self.assertEqual(resp.status_code, 400)
        self.assertIn('Invalid file type', resp.get_json()['error'])

    def test_upload_rejects_oversized_file(self):
        task = self._create_task()
        big = b'0' * (routes.MAX_FILE_SIZE + 1)
        resp = self._upload(task['id'], {'files': (io.BytesIO(big), 'huge.png')})
        self.assertEqual(resp.status_code, 400)
        self.assertIn('File too large', resp.get_json()['error'])

    def test_upload_no_files(self):
        task = self._create_task()
        resp = self._upload(task['id'], {})
        self.assertEqual(resp.status_code, 400)

    def test_upload_task_not_found(self):
        resp = self._upload(9999, {'files': (io.BytesIO(_PNG_BYTES), 'a.png')})
        self.assertEqual(resp.status_code, 404)

    # ---- comment attachments ----

    def _add_comment(self, task_id, data, json=False):
        return self.client.post(
            f'/api/kanban/tasks/{task_id}/comments',
            json=data if json else None,
            data=None if json else data,
            content_type=None if json else 'multipart/form-data',
        )

    def test_add_text_comment_with_json(self):
        task = self._create_task()
        resp = self._add_comment(task['id'], {'content': 'Text only'}, json=True)
        self.assertEqual(resp.status_code, 201)
        comment = resp.get_json()['comment']
        self.assertEqual(comment['content'], 'Text only')
        self.assertEqual(comment['attachments'], [])

    def test_add_comment_with_image_attachment(self):
        task = self._create_task()
        resp = self._add_comment(task['id'], {
            'content': 'See this image',
            'files': (io.BytesIO(_PNG_BYTES), 'comment.png'),
        })
        self.assertEqual(resp.status_code, 201)
        comment = resp.get_json()['comment']
        self.assertEqual(comment['content'], 'See this image')
        self.assertEqual(len(comment['attachments']), 1)
        attachment = comment['attachments'][0]
        self.assertEqual(attachment['filename'], 'comment.png')
        self.assertEqual(attachment['comment_id'], comment['id'])
        self.assertTrue(os.path.isfile(os.path.join(
            self.uploads_dir, f'task_{task["id"]}', attachment['stored_name']
        )))

    def test_comment_attachments_are_returned_and_removed_with_comment(self):
        task = self._create_task()
        added = self._add_comment(task['id'], {
            'files': (io.BytesIO(_PNG_BYTES), 'reference.png'),
        }).get_json()['comment']
        attachment = added['attachments'][0]

        listed = self.client.get(f'/api/kanban/tasks/{task["id"]}/comments').get_json()
        self.assertEqual(listed['comments'][0]['attachments'][0]['id'], attachment['id'])
        self.assertEqual(self.client.get(attachment['url']).status_code, 200)

        deleted = self.client.delete(f'/api/kanban/comments/{added["id"]}')
        self.assertEqual(deleted.status_code, 200)
        self.assertFalse(os.path.exists(os.path.join(
            self.uploads_dir, f'task_{task["id"]}', attachment['stored_name']
        )))
        self.assertIsNone(self.db.get_attachment(attachment['id']))

    # ---- list / serve / delete ----

    def test_list_attachments(self):
        task = self._create_task()
        self._upload(task['id'], {'files': (io.BytesIO(_PNG_BYTES), 'photo.png')})
        resp = self.client.get(f'/api/kanban/tasks/{task["id"]}/attachments')
        self.assertEqual(resp.status_code, 200)
        self.assertEqual(len(resp.get_json()['attachments']), 1)

    def test_serve_attachment_file(self):
        task = self._create_task()
        up = self._upload(task['id'], {'files': (io.BytesIO(_PNG_BYTES), 'photo.png')})
        att = up.get_json()['attachments'][0]
        resp = self.client.get(att['url'])
        self.assertEqual(resp.status_code, 200)
        self.assertEqual(resp.data, _PNG_BYTES)
        self.assertIn('image/png', resp.headers.get('Content-Type', ''))

    def test_serve_missing_attachment(self):
        resp = self.client.get('/api/kanban/attachments/9999/file')
        self.assertEqual(resp.status_code, 404)

    def test_delete_attachment(self):
        task = self._create_task()
        up = self._upload(task['id'], {'files': (io.BytesIO(_PNG_BYTES), 'photo.png')})
        att = up.get_json()['attachments'][0]
        resp = self.client.delete(f'/api/kanban/attachments/{att["id"]}')
        self.assertEqual(resp.status_code, 200)
        listed = self.client.get(f'/api/kanban/tasks/{task["id"]}/attachments').get_json()
        self.assertEqual(listed['attachments'], [])

    def test_delete_task_removes_attachment_files(self):
        task = self._create_task()
        self._upload(task['id'], {'files': (io.BytesIO(_PNG_BYTES), 'photo.png')})
        resp = self.client.delete(f'/api/kanban/tasks/{task["id"]}')
        self.assertEqual(resp.status_code, 200)
        task_dir = os.path.join(self.uploads_dir, f'task_{task["id"]}')
        self.assertFalse(os.path.exists(task_dir))


if __name__ == '__main__':
    unittest.main()

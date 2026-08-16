import unittest
from unittest.mock import patch
import os
import json
import taskloop

class TestTaskLoop(unittest.TestCase):
    def setUp(self):
        # Use a separate file for testing to avoid messing with the actual tasks.json
        self.test_file = "test_tasks.json"
        # Patch the TASKS_FILE in the taskloop module
        self.patcher = patch('taskloop.TASKS_FILE', self.test_file)
        self.patcher.start()
        
        # Ensure the test file is clean before each test
        if os.path.exists(self.test_file):
            os.remove(self.test_file)

    def tearDown(self):
        self.patcher.stop()
        if os.path.exists(self.test_file):
            os.remove(self.test_file)

    def test_add_task(self):
        taskloop.add_task("test task")
        tasks = taskloop.load_tasks()
        self.assertEqual(len(tasks), 1)
        self.assertEqual(tasks[0]["description"], "test task")
        self.assertFalse(tasks[0]["done"])

    def test_list_tasks(self):
        taskloop.add_task("task 1")
        taskloop.add_task("task 2")
        # list_tasks prints to stdout, we can check if it works without crashing
        taskloop.list_tasks()

    def test_done_task(self):
        taskloop.add_task("task to do")
        taskloop.done_task("1")
        tasks = taskloop.load_tasks()
        self.assertTrue(tasks[0]["done"])

if __name__ == "__main__":
    unittest.main()

import unittest
from unittest.mock import patch, MagicMock
import io
import os
import json
from taskloop import add_task, list_tasks, done_task, TASKS_FILE

class TestTaskLoop(unittest.TestCase):
    def setUp(self):
        # Ensure tasks.json is empty or doesn't exist before each test
        if os.path.exists(TASKS_FILE):
            with open(TASKS_FILE, 'w') as f:
                json.dump([], f)

    def tearDown(self):
        # Clean up tasks.json after each test
        if os.path.exists(TASKS_FILE):
            os.remove(TASKS_FILE)

    @patch('builtins.input', return_value='Test task')
    @patch('builtins.print')
    def test_add_task(self, mock_print, mock_input):
        add_task()
        mock_print.assert_called_with('Added task 1.')
        
        with open(TASKS_FILE, 'r') as f:
            tasks = json.load(f)
            self.assertEqual(len(tasks), 1)
            self.assertEqual(tasks[0]['content'], 'Test task')
            self.assertFalse(tasks[0]['done'])

    @patch('builtins.print')
    def test_list_tasks_empty(self, mock_print):
        list_tasks()
        mock_print.assert_called_with('No tasks found.')

    @patch('builtins.input', return_value='1')
    @patch('builtins.print')
    def test_done_task(self, mock_print, mock_input):
        # First add a task
        with patch('builtins.input', return_value='Initial task'):
            add_task()
        
        # Now mark it as done
        done_task()
        mock_print.assert_called_with('Marked task 1 as done.')
        
        with open(TASKS_FILE, 'r') as f:
            tasks = json.load(f)
            self.assertTrue(tasks[0]['done'])

if __name__ == '__main__':
    unittest.main()

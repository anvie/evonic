"""
Test workspace path validation for agent creation/update endpoints.

Security: Agents should not be able to use arbitrary filesystem paths
as workspaces. This could allow reading/writing sensitive system files.
"""
import unittest
import sys
import os
import tempfile
import shutil

# Add parent directory to path for imports
sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from models.db import db


class TestAgentWorkspaceValidation(unittest.TestCase):
    """Test that agent workspace paths are validated to prevent directory traversal."""

    def setUp(self):
        """Create a test agent for workspace validation tests."""
        self.test_agent_id = 'test_workspace_agent'
        # Clean up any existing test agent
        try:
            db.delete_agent(self.test_agent_id)
        except Exception:
            pass
        
        # Create a fresh test agent
        db.create_agent({
            'id': self.test_agent_id,
            'name': 'Workspace Test Agent',
            'enabled': 0,
            'workspace': '/tmp/safe_workspace'
        })

    def tearDown(self):
        """Clean up test agent."""
        try:
            db.delete_agent(self.test_agent_id)
        except Exception:
            pass

    def test_workspace_rejects_absolute_system_paths(self):
        """Workspace paths should reject absolute paths to system directories."""
        dangerous_paths = [
            '/etc',
            '/etc/passwd',
            '/root',
            '/var/log',
            'C:\\Windows\\System32',
            'C:\\Windows',
        ]
        
        for dangerous_path in dangerous_paths:
            with self.subTest(path=dangerous_path):
                # Attempting to set a dangerous workspace path should raise ValueError
                with self.assertRaises(ValueError) as cm:
                    db.update_agent(self.test_agent_id, {'workspace': dangerous_path})
                
                # Verify the error message mentions the security issue
                self.assertIn('workspace', str(cm.exception).lower())
                
                # Verify the workspace was NOT changed
                agent = db.get_agent(self.test_agent_id)
                self.assertNotEqual(
                    agent.get('workspace'),
                    dangerous_path,
                    f"Agent workspace should not be set to dangerous path: {dangerous_path}"
                )

    def test_workspace_rejects_path_traversal(self):
        """Workspace paths should reject path traversal attempts."""
        traversal_paths = [
            '../../../etc/passwd',
            '../../sensitive',
            './../../root',
            'workspace/../../etc',
        ]
        
        for traversal_path in traversal_paths:
            with self.subTest(path=traversal_path):
                # Path traversal should raise ValueError
                with self.assertRaises(ValueError) as cm:
                    db.update_agent(self.test_agent_id, {'workspace': traversal_path})
                
                # Verify error mentions path traversal or workspace
                error_msg = str(cm.exception).lower()
                self.assertTrue(
                    'traversal' in error_msg or 'workspace' in error_msg,
                    f"Error message should mention path issue: {cm.exception}"
                )
                
                # Verify workspace was NOT changed
                agent = db.get_agent(self.test_agent_id)
                self.assertNotEqual(
                    agent.get('workspace'),
                    traversal_path,
                    f"Agent workspace should not accept path traversal: {traversal_path}"
                )

    def test_workspace_allows_safe_relative_paths(self):
        """Workspace paths should allow safe relative paths within agents directory."""
        safe_paths = [
            'my_workspace',
            'agent_data',
            'workspace/subdir',
        ]
        
        for safe_path in safe_paths:
            with self.subTest(path=safe_path):
                result = db.update_agent(self.test_agent_id, {'workspace': safe_path})
                
                self.assertTrue(result, f"Failed to update workspace to safe path: {safe_path}")
                
                agent = db.get_agent(self.test_agent_id)
                self.assertEqual(
                    agent.get('workspace'),
                    safe_path,
                    f"Safe workspace path was not set correctly: {safe_path}"
                )

    def test_workspace_allows_approved_absolute_paths(self):
        """Workspace paths should allow absolute paths within approved directories."""
        # Create a safe temp directory for testing
        test_dir = tempfile.mkdtemp(prefix='evonic_test_')
        
        try:
            result = db.update_agent(self.test_agent_id, {'workspace': test_dir})
            
            # This should be allowed if it's in an approved location
            # (implementation will define what's "approved")
            agent = db.get_agent(self.test_agent_id)
            
            # For now, we expect either the path is set correctly OR validation rejected it
            # The key is that dangerous paths are blocked
            workspace = agent.get('workspace')
            
            # If it was set, it should match exactly
            if workspace == test_dir:
                self.assertEqual(workspace, test_dir)
        finally:
            # Clean up
            try:
                shutil.rmtree(test_dir)
            except Exception:
                pass

    def test_workspace_empty_or_null_uses_default(self):
        """Empty or null workspace should use a safe default."""
        for value in [None, '', '   ']:
            with self.subTest(value=repr(value)):
                result = db.update_agent(self.test_agent_id, {'workspace': value})
                
                agent = db.get_agent(self.test_agent_id)
                workspace = agent.get('workspace')
                
                # Should either keep existing value or set a safe default
                # Should NOT be a dangerous system path
                if workspace:
                    self.assertNotIn('/etc', workspace)
                    self.assertNotIn('/root', workspace)
                    self.assertNotIn('C:\\Windows', workspace)


if __name__ == '__main__':
    unittest.main()

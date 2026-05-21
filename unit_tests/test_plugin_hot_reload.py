"""
Unit tests for plugin hot reload functionality.
"""

import os
import time
import tempfile
import shutil
import pytest
from unittest.mock import Mock, patch, MagicMock
from backend.plugin_hot_reload import PluginHotReloadManager, DEBOUNCE_DELAY


class TestPluginHotReloadManager:
    """Test the PluginHotReloadManager class."""
    
    def test_initialization(self):
        """Test manager initialization."""
        manager = PluginHotReloadManager()
        assert not manager.is_enabled()
        assert manager.get_status()['watched_plugins'] == []
    
    def test_enable_disable_globally(self):
        """Test global enable/disable."""
        manager = PluginHotReloadManager()
        
        assert not manager.is_enabled()
        
        manager.enable_globally()
        assert manager.is_enabled()
        
        manager.disable_globally()
        assert not manager.is_enabled()
    
    def test_enable_for_nonexistent_plugin(self):
        """Test enabling hot reload for non-existent plugin."""
        manager = PluginHotReloadManager()
        
        success = manager.enable_for_plugin('nonexistent_plugin_xyz')
        assert not success
    
    def test_enable_for_plugin(self):
        """Test enabling hot reload for a plugin."""
        manager = PluginHotReloadManager()
        manager.enable_globally()  # Enable globally first
        
        # Create temporary plugin directory
        with tempfile.TemporaryDirectory() as temp_dir:
            plugin_id = 'test_plugin'
            plugin_dir = os.path.join(temp_dir, plugin_id)
            os.makedirs(plugin_dir)
            
            # Create a dummy plugin file
            with open(os.path.join(plugin_dir, 'handler.py'), 'w') as f:
                f.write('# test plugin\n')
            
            # Patch PLUGINS_DIR
            with patch('backend.plugin_hot_reload.PLUGINS_DIR', temp_dir):
                success = manager.enable_for_plugin(plugin_id)
                assert success
                
                status = manager.get_status()
                assert plugin_id in status['watched_plugins']
                assert status['active_watchers'] == 1
                
                # Cleanup
                manager.disable_for_plugin(plugin_id)
    
    def test_disable_for_plugin(self):
        """Test disabling hot reload for a plugin."""
        manager = PluginHotReloadManager()
        manager.enable_globally()  # Enable globally first
        
        with tempfile.TemporaryDirectory() as temp_dir:
            plugin_id = 'test_plugin'
            plugin_dir = os.path.join(temp_dir, plugin_id)
            os.makedirs(plugin_dir)
            
            with open(os.path.join(plugin_dir, 'handler.py'), 'w') as f:
                f.write('# test plugin\n')
            
            with patch('backend.plugin_hot_reload.PLUGINS_DIR', temp_dir):
                manager.enable_for_plugin(plugin_id)
                assert plugin_id in manager.get_status()['watched_plugins']
                
                success = manager.disable_for_plugin(plugin_id)
                assert success
                assert plugin_id not in manager.get_status()['watched_plugins']
    
    def test_disable_not_enabled_plugin(self):
        """Test disabling hot reload for plugin that wasn't enabled."""
        manager = PluginHotReloadManager()
        
        success = manager.disable_for_plugin('not_enabled_plugin')
        assert not success
    
    def test_enable_same_plugin_twice(self):
        """Test enabling hot reload twice for same plugin."""
        manager = PluginHotReloadManager()
        manager.enable_globally()  # Enable globally first
        
        with tempfile.TemporaryDirectory() as temp_dir:
            plugin_id = 'test_plugin'
            plugin_dir = os.path.join(temp_dir, plugin_id)
            os.makedirs(plugin_dir)
            
            with open(os.path.join(plugin_dir, 'handler.py'), 'w') as f:
                f.write('# test plugin\n')
            
            with patch('backend.plugin_hot_reload.PLUGINS_DIR', temp_dir):
                success1 = manager.enable_for_plugin(plugin_id)
                assert success1
                
                success2 = manager.enable_for_plugin(plugin_id)
                assert success2
                
                # Should still have only one watcher
                status = manager.get_status()
                assert status['active_watchers'] == 1
                
                manager.disable_for_plugin(plugin_id)
    
    def test_file_change_detection(self):
        """Test that file changes are detected."""
        manager = PluginHotReloadManager()
        manager.enable_globally()  # Enable globally first
        mock_pm = Mock()
        manager._plugin_manager = mock_pm
        
        with tempfile.TemporaryDirectory() as temp_dir:
            plugin_id = 'test_plugin'
            plugin_dir = os.path.join(temp_dir, plugin_id)
            os.makedirs(plugin_dir)
            
            handler_path = os.path.join(plugin_dir, 'handler.py')
            with open(handler_path, 'w') as f:
                f.write('# version 1\n')
            
            with patch('backend.plugin_hot_reload.PLUGINS_DIR', temp_dir):
                manager.enable_for_plugin(plugin_id)
                
                # Wait for initial scan
                time.sleep(0.6)
                
                # Modify file
                time.sleep(0.1)  # Ensure mtime changes
                with open(handler_path, 'w') as f:
                    f.write('# version 2\n')
                
                # Wait for detection + debounce + reload
                time.sleep(DEBOUNCE_DELAY + 1.5)
                
                # Check that reload was called
                assert mock_pm.reload_plugin.called
                assert mock_pm.reload_plugin.call_args[0][0] == plugin_id
                
                manager.disable_for_plugin(plugin_id)
    
    def test_new_file_detection(self):
        """Test that new files are detected."""
        manager = PluginHotReloadManager()
        manager.enable_globally()  # Enable globally first
        mock_pm = Mock()
        manager._plugin_manager = mock_pm
        
        with tempfile.TemporaryDirectory() as temp_dir:
            plugin_id = 'test_plugin'
            plugin_dir = os.path.join(temp_dir, plugin_id)
            os.makedirs(plugin_dir)
            
            with open(os.path.join(plugin_dir, 'handler.py'), 'w') as f:
                f.write('# initial\n')
            
            with patch('backend.plugin_hot_reload.PLUGINS_DIR', temp_dir):
                manager.enable_for_plugin(plugin_id)
                
                # Wait for initial scan
                time.sleep(0.6)
                
                # Add new file
                with open(os.path.join(plugin_dir, 'utils.py'), 'w') as f:
                    f.write('# new file\n')
                
                # Wait for detection + debounce + reload
                time.sleep(DEBOUNCE_DELAY + 1.5)
                
                # Check that reload was called
                assert mock_pm.reload_plugin.called
                
                manager.disable_for_plugin(plugin_id)
    
    def test_deleted_file_detection(self):
        """Test that deleted files are detected."""
        manager = PluginHotReloadManager()
        manager.enable_globally()  # Enable globally first
        mock_pm = Mock()
        manager._plugin_manager = mock_pm
        
        with tempfile.TemporaryDirectory() as temp_dir:
            plugin_id = 'test_plugin'
            plugin_dir = os.path.join(temp_dir, plugin_id)
            os.makedirs(plugin_dir)
            
            handler_path = os.path.join(plugin_dir, 'handler.py')
            utils_path = os.path.join(plugin_dir, 'utils.py')
            
            with open(handler_path, 'w') as f:
                f.write('# handler\n')
            with open(utils_path, 'w') as f:
                f.write('# utils\n')
            
            with patch('backend.plugin_hot_reload.PLUGINS_DIR', temp_dir):
                manager.enable_for_plugin(plugin_id)
                
                # Wait for initial scan
                time.sleep(0.6)
                
                # Delete file
                os.unlink(utils_path)
                
                # Wait for detection + debounce + reload
                time.sleep(DEBOUNCE_DELAY + 1.5)
                
                # Check that reload was called
                assert mock_pm.reload_plugin.called
                
                manager.disable_for_plugin(plugin_id)
    
    def test_ignores_pycache(self):
        """Test that __pycache__ directories are ignored."""
        manager = PluginHotReloadManager()
        
        with tempfile.TemporaryDirectory() as temp_dir:
            plugin_id = 'test_plugin'
            plugin_dir = os.path.join(temp_dir, plugin_id)
            os.makedirs(plugin_dir)
            
            # Create __pycache__ directory
            pycache_dir = os.path.join(plugin_dir, '__pycache__')
            os.makedirs(pycache_dir)
            
            with open(os.path.join(plugin_dir, 'handler.py'), 'w') as f:
                f.write('# handler\n')
            
            with open(os.path.join(pycache_dir, 'handler.cpython-39.pyc'), 'wb') as f:
                f.write(b'\x00' * 100)
            
            with patch('backend.plugin_hot_reload.PLUGINS_DIR', temp_dir):
                manager.enable_for_plugin(plugin_id)
                
                # Wait for initial scan
                time.sleep(0.6)
                
                # Check that .pyc files are not tracked
                mtimes = manager._file_mtimes.get(plugin_id, {})
                pyc_files = [f for f in mtimes.keys() if f.endswith('.pyc')]
                assert len(pyc_files) == 0
                
                manager.disable_for_plugin(plugin_id)
    
    def test_only_watches_specific_extensions(self):
        """Test that only specific file extensions are watched."""
        manager = PluginHotReloadManager()
        manager.enable_globally()  # Enable globally first
        
        with tempfile.TemporaryDirectory() as temp_dir:
            plugin_id = 'test_plugin'
            plugin_dir = os.path.join(temp_dir, plugin_id)
            os.makedirs(plugin_dir)
            
            # Create files with different extensions
            with open(os.path.join(plugin_dir, 'handler.py'), 'w') as f:
                f.write('# python\n')
            with open(os.path.join(plugin_dir, 'config.json'), 'w') as f:
                f.write('{}\n')
            with open(os.path.join(plugin_dir, 'readme.md'), 'w') as f:
                f.write('# readme\n')
            with open(os.path.join(plugin_dir, 'data.txt'), 'w') as f:
                f.write('data\n')
            with open(os.path.join(plugin_dir, 'image.png'), 'wb') as f:
                f.write(b'\x00' * 100)
            
            with patch('backend.plugin_hot_reload.PLUGINS_DIR', temp_dir):
                manager.enable_for_plugin(plugin_id)
                
                # Wait for initial scan
                time.sleep(0.6)
                
                # Check tracked files
                mtimes = manager._file_mtimes.get(plugin_id, {})
                tracked_files = [os.path.basename(f) for f in mtimes.keys()]
                
                assert 'handler.py' in tracked_files
                assert 'config.json' in tracked_files
                assert 'readme.md' in tracked_files
                assert 'image.png' not in tracked_files  # Binary files not watched
                
                manager.disable_for_plugin(plugin_id)
    
    def test_get_status(self):
        """Test getting hot reload status."""
        manager = PluginHotReloadManager()
        
        status = manager.get_status()
        assert 'enabled' in status
        assert 'watched_plugins' in status
        assert 'pending_reloads' in status
        assert 'active_watchers' in status
        
        assert isinstance(status['enabled'], bool)
        assert isinstance(status['watched_plugins'], list)
        assert isinstance(status['pending_reloads'], dict)
        assert isinstance(status['active_watchers'], int)
    
    def test_shutdown(self):
        """Test shutting down hot reload manager."""
        manager = PluginHotReloadManager()
        
        manager.enable_globally()
        assert manager.is_enabled()
        
        manager.shutdown()
        assert not manager.is_enabled()


if __name__ == '__main__':
    pytest.main([__file__, '-v'])

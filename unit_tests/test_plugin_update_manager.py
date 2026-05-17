"""
Unit tests for plugin and skill update manager.

Tests version comparison logic, update checking, and status retrieval.
"""

import pytest
from unittest.mock import Mock, patch, MagicMock
from backend.plugin_update_manager import (
    _parse_version,
    compare_versions,
    PluginUpdateManager,
    SkillUpdateManager,
    plugin_update_manager,
    skill_update_manager,
)


class TestVersionParsing:
    """Test version string parsing."""
    
    def test_parse_standard_semver(self):
        """Test parsing standard semantic versions."""
        assert _parse_version("1.0.0") == (1, 0, 0, 0)
        assert _parse_version("2.1.3") == (2, 1, 3, 0)
        assert _parse_version("10.20.30") == (10, 20, 30, 0)
    
    def test_parse_short_versions(self):
        """Test parsing versions with fewer than 3 parts."""
        assert _parse_version("1.0") == (1, 0, 0, 0)
        assert _parse_version("2") == (2, 0, 0, 0)
        assert _parse_version("3.5") == (3, 5, 0, 0)
    
    def test_parse_with_v_prefix(self):
        """Test parsing versions with 'v' prefix."""
        assert _parse_version("v1.0.0") == (1, 0, 0, 0)
        assert _parse_version("V2.1.3") == (2, 1, 3, 0)
        assert _parse_version("v3.5") == (3, 5, 0, 0)
    
    def test_parse_prerelease_versions(self):
        """Test parsing pre-release versions."""
        assert _parse_version("1.0.0-beta") == (1, 0, 0, -1)
        assert _parse_version("2.1.0-alpha") == (2, 1, 0, -1)
        assert _parse_version("1.0.0-rc.1") == (1, 0, 0, -1)
    
    def test_parse_invalid_versions(self):
        """Test parsing invalid version strings."""
        assert _parse_version("") == (0, 0, 0, 0)
        assert _parse_version(None) == (0, 0, 0, 0)
        assert _parse_version("invalid") == (0, 0, 0, 0)
        assert _parse_version("abc.def.ghi") == (0, 0, 0, 0)
    
    def test_parse_with_whitespace(self):
        """Test parsing versions with whitespace."""
        assert _parse_version("  1.0.0  ") == (1, 0, 0, 0)
        assert _parse_version("\t2.1.3\n") == (2, 1, 3, 0)


class TestVersionComparison:
    """Test version comparison logic."""
    
    def test_compare_equal_versions(self):
        """Test comparing equal versions."""
        assert compare_versions("1.0.0", "1.0.0") == 0
        assert compare_versions("2.1.3", "2.1.3") == 0
        assert compare_versions("v1.0.0", "1.0.0") == 0
    
    def test_compare_newer_version_available(self):
        """Test when update is available (latest > current)."""
        assert compare_versions("1.0.0", "1.0.1") == 1
        assert compare_versions("1.0.0", "1.1.0") == 1
        assert compare_versions("1.0.0", "2.0.0") == 1
        assert compare_versions("1.9.9", "2.0.0") == 1
    
    def test_compare_current_is_newer(self):
        """Test when current version is newer than latest."""
        assert compare_versions("1.0.1", "1.0.0") == -1
        assert compare_versions("2.0.0", "1.9.9") == -1
        assert compare_versions("1.1.0", "1.0.5") == -1
    
    def test_compare_prerelease_versions(self):
        """Test comparing pre-release versions."""
        # Pre-release should be less than stable (so stable is an "update")
        assert compare_versions("1.0.0-beta", "1.0.0") == 1  # beta -> stable is an update
        assert compare_versions("1.0.0", "1.0.0-beta") == -1  # stable -> beta is downgrade
        
        # Pre-release versions are equal to each other (we don't parse the suffix detail)
        assert compare_versions("1.0.0-alpha", "1.0.0-beta") == 0
    
    def test_compare_short_versions(self):
        """Test comparing versions with different lengths."""
        assert compare_versions("1.0", "1.0.0") == 0
        assert compare_versions("1", "1.0.0") == 0
        assert compare_versions("2.1", "2.1.3") == 1
    
    def test_compare_with_v_prefix(self):
        """Test comparing versions with 'v' prefix."""
        assert compare_versions("v1.0.0", "v1.0.1") == 1
        assert compare_versions("1.0.0", "v1.0.0") == 0


class TestPluginUpdateManager:
    """Test PluginUpdateManager functionality."""
    
    @patch('backend.plugin_lifecycle.PluginManager')
    def test_check_plugin_updates_no_updates(self, mock_pm_class):
        """Test checking for updates when none are available."""
        mock_pm = Mock()
        mock_pm.list_plugins.return_value = [
            {
                'id': 'test-plugin',
                'name': 'Test Plugin',
                'version': '1.0.0',
                'category': 'general',
            }
        ]
        mock_pm_class.return_value = mock_pm
        
        manager = PluginUpdateManager()
        updates = manager.check_plugin_updates()
        
        assert updates == []
    
    @patch('backend.plugin_lifecycle.PluginManager')
    def test_check_plugin_updates_with_updates(self, mock_pm_class):
        """Test checking for updates when updates are available."""
        mock_pm = Mock()
        mock_pm.list_plugins.return_value = [
            {
                'id': 'test-plugin',
                'name': 'Test Plugin',
                'version': '1.0.0',
                'category': 'general',
                'update': {
                    'latest_version': '1.1.0',
                    'url': 'https://example.com/update.zip',
                }
            }
        ]
        mock_pm_class.return_value = mock_pm
        
        manager = PluginUpdateManager()
        updates = manager.check_plugin_updates()
        
        assert len(updates) == 1
        assert updates[0]['id'] == 'test-plugin'
        assert updates[0]['current_version'] == '1.0.0'
        assert updates[0]['latest_version'] == '1.1.0'
        assert updates[0]['update_available'] is True
        assert updates[0]['update_url'] == 'https://example.com/update.zip'
    
    @patch('backend.plugin_lifecycle.PluginManager')
    def test_check_plugin_updates_current_is_newer(self, mock_pm_class):
        """Test when current version is newer than 'latest'."""
        mock_pm = Mock()
        mock_pm.list_plugins.return_value = [
            {
                'id': 'test-plugin',
                'name': 'Test Plugin',
                'version': '2.0.0',
                'category': 'general',
                'update': {
                    'latest_version': '1.5.0',
                    'url': 'https://example.com/update.zip',
                }
            }
        ]
        mock_pm_class.return_value = mock_pm
        
        manager = PluginUpdateManager()
        updates = manager.check_plugin_updates()
        
        # Should not include plugins where current > latest
        assert updates == []
    
    @patch('backend.plugin_lifecycle.PluginManager')
    def test_check_plugin_updates_multiple_plugins(self, mock_pm_class):
        """Test checking updates for multiple plugins."""
        mock_pm = Mock()
        mock_pm.list_plugins.return_value = [
            {
                'id': 'plugin-1',
                'name': 'Plugin 1',
                'version': '1.0.0',
                'category': 'general',
                'update': {
                    'latest_version': '1.1.0',
                    'url': 'https://example.com/p1.zip',
                }
            },
            {
                'id': 'plugin-2',
                'name': 'Plugin 2',
                'version': '2.0.0',
                'category': 'tools',
                # No update info
            },
            {
                'id': 'plugin-3',
                'name': 'Plugin 3',
                'version': '1.5.0',
                'category': 'general',
                'update': {
                    'latest_version': '2.0.0',
                    'url': 'https://example.com/p3.zip',
                }
            },
        ]
        mock_pm_class.return_value = mock_pm
        
        manager = PluginUpdateManager()
        updates = manager.check_plugin_updates()
        
        assert len(updates) == 2
        assert updates[0]['id'] == 'plugin-1'
        assert updates[1]['id'] == 'plugin-3'
    
    @patch('backend.plugin_lifecycle.PluginManager')
    def test_get_plugin_update_status_not_found(self, mock_pm_class):
        """Test getting update status for non-existent plugin."""
        mock_pm = Mock()
        mock_pm.get_plugin.return_value = None
        mock_pm_class.return_value = mock_pm
        
        manager = PluginUpdateManager()
        status = manager.get_plugin_update_status('nonexistent')
        
        assert 'error' in status
        assert 'not found' in status['error'].lower()
    
    @patch('backend.plugin_lifecycle.PluginManager')
    def test_get_plugin_update_status_no_update_info(self, mock_pm_class):
        """Test getting update status when no update info is available."""
        mock_pm = Mock()
        mock_pm.get_plugin.return_value = {
            'id': 'test-plugin',
            'name': 'Test Plugin',
            'version': '1.0.0',
        }
        mock_pm_class.return_value = mock_pm
        
        manager = PluginUpdateManager()
        status = manager.get_plugin_update_status('test-plugin')
        
        assert status['id'] == 'test-plugin'
        assert status['current_version'] == '1.0.0'
        assert status['update_available'] is False
        assert 'No update information' in status['message']
    
    @patch('backend.plugin_lifecycle.PluginManager')
    def test_get_plugin_update_status_with_update(self, mock_pm_class):
        """Test getting update status when update is available."""
        mock_pm = Mock()
        mock_pm.get_plugin.return_value = {
            'id': 'test-plugin',
            'name': 'Test Plugin',
            'version': '1.0.0',
            'update': {
                'latest_version': '1.1.0',
                'url': 'https://example.com/update.zip',
                'changelog': 'Bug fixes and improvements',
            }
        }
        mock_pm_class.return_value = mock_pm
        
        manager = PluginUpdateManager()
        status = manager.get_plugin_update_status('test-plugin')
        
        assert status['id'] == 'test-plugin'
        assert status['current_version'] == '1.0.0'
        assert status['latest_version'] == '1.1.0'
        assert status['update_available'] is True
        assert status['update_url'] == 'https://example.com/update.zip'
        assert status['changelog'] == 'Bug fixes and improvements'


class TestSkillUpdateManager:
    """Test SkillUpdateManager functionality."""
    
    def test_check_skill_updates_no_updates(self):
        """Test checking for skill updates when none are available."""
        with patch('backend.skills_manager.skills_manager') as mock_sm:
            mock_sm.list_skills.return_value = [
                {
                    'id': 'test-skill',
                    'name': 'Test Skill',
                    'version': '1.0.0',
                }
            ]
            
            manager = SkillUpdateManager()
            updates = manager.check_skill_updates()
            
            assert updates == []
    
    def test_check_skill_updates_with_updates(self):
        """Test checking for skill updates when updates are available."""
        with patch('backend.skills_manager.skills_manager') as mock_sm:
            mock_sm.list_skills.return_value = [
                {
                    'id': 'test-skill',
                    'name': 'Test Skill',
                    'version': '1.0.0',
                    'update': {
                        'latest_version': '1.2.0',
                        'url': 'https://example.com/skill-update.zip',
                    }
                }
            ]
            
            manager = SkillUpdateManager()
            updates = manager.check_skill_updates()
            
            assert len(updates) == 1
            assert updates[0]['id'] == 'test-skill'
            assert updates[0]['current_version'] == '1.0.0'
            assert updates[0]['latest_version'] == '1.2.0'
            assert updates[0]['update_available'] is True
    
    def test_get_skill_update_status_not_found(self):
        """Test getting update status for non-existent skill."""
        with patch('backend.skills_manager.skills_manager') as mock_sm:
            mock_sm.get_skill.return_value = None
            
            manager = SkillUpdateManager()
            status = manager.get_skill_update_status('nonexistent')
            
            assert 'error' in status
            assert 'not found' in status['error'].lower()
    
    def test_get_skill_update_status_with_update(self):
        """Test getting update status when update is available."""
        with patch('backend.skills_manager.skills_manager') as mock_sm:
            mock_sm.get_skill.return_value = {
                'id': 'test-skill',
                'name': 'Test Skill',
                'version': '1.0.0',
                'update': {
                    'latest_version': '1.5.0',
                    'url': 'https://example.com/skill.zip',
                    'changelog': 'New features added',
                }
            }
            
            manager = SkillUpdateManager()
            status = manager.get_skill_update_status('test-skill')
            
            assert status['id'] == 'test-skill'
            assert status['current_version'] == '1.0.0'
            assert status['latest_version'] == '1.5.0'
            assert status['update_available'] is True
            assert status['changelog'] == 'New features added'


class TestSingletonInstances:
    """Test that singleton instances are properly initialized."""
    
    def test_plugin_update_manager_singleton(self):
        """Test plugin_update_manager singleton exists."""
        assert plugin_update_manager is not None
        assert isinstance(plugin_update_manager, PluginUpdateManager)
    
    def test_skill_update_manager_singleton(self):
        """Test skill_update_manager singleton exists."""
        assert skill_update_manager is not None
        assert isinstance(skill_update_manager, SkillUpdateManager)

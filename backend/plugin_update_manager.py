"""
Plugin and Skill Update Manager

Provides update checking and installation for plugins and skills.
Supports version comparison and update notifications.
"""

import logging
import os
import re
from typing import Dict, Any, List, Optional, Tuple

_logger = logging.getLogger(__name__)


def _parse_version(version_str: str) -> Tuple[int, ...]:
    """
    Parse a semantic version string into a comparable tuple.
    
    Examples:
        "1.0.0" -> (1, 0, 0, 0)
        "2.1" -> (2, 1, 0, 0)
        "1.0.0-beta" -> (1, 0, 0, -1)  # pre-release versions sort lower
    
    Returns:
        Tuple of integers for comparison. Invalid versions return (0, 0, 0, 0).
    """
    if not version_str or not isinstance(version_str, str):
        return (0, 0, 0, 0)
    
    # Remove 'v' prefix if present
    version_str = version_str.strip().lower()
    if version_str.startswith('v'):
        version_str = version_str[1:]
    
    # Split on '-' to separate pre-release suffix
    parts = version_str.split('-', 1)
    version_part = parts[0]
    is_prerelease = len(parts) > 1
    
    # Parse numeric version parts
    try:
        numbers = []
        for part in version_part.split('.'):
            if part.isdigit():
                numbers.append(int(part))
            else:
                break
        
        # Pad to at least 3 parts
        while len(numbers) < 3:
            numbers.append(0)
        
        # Add pre-release marker (sorts before stable)
        # Use -1 for pre-release, 0 for stable so pre-release < stable
        if is_prerelease:
            numbers.append(-1)
        else:
            numbers.append(0)
        
        return tuple(numbers)
    except (ValueError, AttributeError):
        return (0, 0, 0, 0)


def compare_versions(current: str, latest: str) -> int:
    """
    Compare two version strings.
    
    Returns:
        1 if latest > current (update available)
        0 if latest == current (up to date)
        -1 if latest < current (current is newer)
    """
    current_tuple = _parse_version(current)
    latest_tuple = _parse_version(latest)
    
    if latest_tuple > current_tuple:
        return 1
    elif latest_tuple < current_tuple:
        return -1
    else:
        return 0


class PluginUpdateManager:
    """Manages plugin updates and version checking."""
    
    def __init__(self):
        from backend.plugin_lifecycle import PluginManager
        self.plugin_manager = PluginManager()
    
    def check_plugin_updates(self) -> List[Dict[str, Any]]:
        """
        Check for available updates for all installed plugins.
        
        Returns:
            List of plugins with available updates, each containing:
            - id: plugin identifier
            - name: plugin display name
            - current_version: currently installed version
            - latest_version: available version
            - update_available: boolean
            - update_url: URL to download update (if available)
        """
        updates = []
        plugins = self.plugin_manager.list_plugins()
        
        for plugin in plugins:
            plugin_id = plugin.get('id', '')
            current_version = plugin.get('version', '0.0.0')
            
            # Check for update info in manifest
            update_info = plugin.get('update', {})
            if not update_info:
                continue
            
            latest_version = update_info.get('latest_version')
            update_url = update_info.get('url')
            
            if not latest_version:
                continue
            
            # Compare versions
            comparison = compare_versions(current_version, latest_version)
            
            if comparison > 0:  # Update available
                updates.append({
                    'id': plugin_id,
                    'name': plugin.get('name', plugin_id),
                    'current_version': current_version,
                    'latest_version': latest_version,
                    'update_available': True,
                    'update_url': update_url,
                    'category': plugin.get('category', 'general'),
                })
        
        return updates
    
    def get_plugin_update_status(self, plugin_id: str) -> Dict[str, Any]:
        """
        Get update status for a specific plugin.
        
        Returns:
            Dictionary with update information or error.
        """
        plugin = self.plugin_manager.get_plugin(plugin_id)
        if not plugin:
            return {'error': f'Plugin not found: {plugin_id}'}
        
        current_version = plugin.get('version', '0.0.0')
        update_info = plugin.get('update', {})
        
        if not update_info:
            return {
                'id': plugin_id,
                'current_version': current_version,
                'update_available': False,
                'message': 'No update information available',
            }
        
        latest_version = update_info.get('latest_version')
        if not latest_version:
            return {
                'id': plugin_id,
                'current_version': current_version,
                'update_available': False,
                'message': 'No version information in update manifest',
            }
        
        comparison = compare_versions(current_version, latest_version)
        
        return {
            'id': plugin_id,
            'name': plugin.get('name', plugin_id),
            'current_version': current_version,
            'latest_version': latest_version,
            'update_available': comparison > 0,
            'update_url': update_info.get('url'),
            'changelog': update_info.get('changelog'),
        }


class SkillUpdateManager:
    """Manages skill updates and version checking."""
    
    def __init__(self):
        from backend.skills_manager import skills_manager
        self.skills_manager = skills_manager
    
    def check_skill_updates(self) -> List[Dict[str, Any]]:
        """
        Check for available updates for all installed skills.
        
        Returns:
            List of skills with available updates.
        """
        updates = []
        skills = self.skills_manager.list_skills()
        
        for skill in skills:
            skill_id = skill.get('id', '')
            current_version = skill.get('version', '0.0.0')
            
            # Check for update info in manifest
            update_info = skill.get('update', {})
            if not update_info:
                continue
            
            latest_version = update_info.get('latest_version')
            update_url = update_info.get('url')
            
            if not latest_version:
                continue
            
            # Compare versions
            comparison = compare_versions(current_version, latest_version)
            
            if comparison > 0:  # Update available
                updates.append({
                    'id': skill_id,
                    'name': skill.get('name', skill_id),
                    'current_version': current_version,
                    'latest_version': latest_version,
                    'update_available': True,
                    'update_url': update_url,
                })
        
        return updates
    
    def get_skill_update_status(self, skill_id: str) -> Dict[str, Any]:
        """
        Get update status for a specific skill.
        
        Returns:
            Dictionary with update information or error.
        """
        skill = self.skills_manager.get_skill(skill_id)
        if not skill:
            return {'error': f'Skill not found: {skill_id}'}
        
        current_version = skill.get('version', '0.0.0')
        update_info = skill.get('update', {})
        
        if not update_info:
            return {
                'id': skill_id,
                'current_version': current_version,
                'update_available': False,
                'message': 'No update information available',
            }
        
        latest_version = update_info.get('latest_version')
        if not latest_version:
            return {
                'id': skill_id,
                'current_version': current_version,
                'update_available': False,
                'message': 'No version information in update manifest',
            }
        
        comparison = compare_versions(current_version, latest_version)
        
        return {
            'id': skill_id,
            'name': skill.get('name', skill_id),
            'current_version': current_version,
            'latest_version': latest_version,
            'update_available': comparison > 0,
            'update_url': update_info.get('url'),
            'changelog': update_info.get('changelog'),
        }


# Singleton instances
plugin_update_manager = PluginUpdateManager()
skill_update_manager = SkillUpdateManager()

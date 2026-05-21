"""
Unit tests for plugin signature verification.
"""

import os
import tempfile
import zipfile
import pytest
from unittest.mock import patch, MagicMock
from backend.plugin_signer import (
    is_gpg_available,
    has_trusted_keys,
    is_signature_verification_enabled,
    verify_plugin_signature,
    get_signature_status,
    SignatureVerificationError,
    TRUSTED_KEYS_FILE
)


class TestGPGAvailability:
    """Test GPG availability detection."""
    
    def test_is_gpg_available_when_installed(self):
        """Test GPG detection when gpg is installed."""
        # This test will pass or fail based on actual system state
        # We just verify the function returns a boolean
        result = is_gpg_available()
        assert isinstance(result, bool)
    
    @patch('subprocess.run')
    def test_is_gpg_available_when_not_installed(self, mock_run):
        """Test GPG detection when gpg is not installed."""
        mock_run.side_effect = FileNotFoundError()
        result = is_gpg_available()
        assert result is False
    
    @patch('subprocess.run')
    def test_is_gpg_available_when_timeout(self, mock_run):
        """Test GPG detection when command times out."""
        import subprocess
        mock_run.side_effect = subprocess.TimeoutExpired('gpg', 5)
        result = is_gpg_available()
        assert result is False


class TestTrustedKeys:
    """Test trusted keys detection."""
    
    def test_has_trusted_keys_when_missing(self):
        """Test trusted keys detection when file doesn't exist."""
        # Temporarily patch TRUSTED_KEYS_FILE to non-existent path
        with patch('backend.plugin_signer.TRUSTED_KEYS_FILE', '/nonexistent/path/trusted_keys.asc'):
            result = has_trusted_keys()
            assert result is False
    
    def test_has_trusted_keys_when_empty(self):
        """Test trusted keys detection when file is empty."""
        with tempfile.NamedTemporaryFile(mode='w', delete=False, suffix='.asc') as f:
            temp_path = f.name
        
        try:
            with patch('backend.plugin_signer.TRUSTED_KEYS_FILE', temp_path):
                result = has_trusted_keys()
                assert result is False
        finally:
            os.unlink(temp_path)
    
    def test_has_trusted_keys_when_exists(self):
        """Test trusted keys detection when file exists with content."""
        with tempfile.NamedTemporaryFile(mode='w', delete=False, suffix='.asc') as f:
            f.write("-----BEGIN PGP PUBLIC KEY BLOCK-----\ntest\n-----END PGP PUBLIC KEY BLOCK-----\n")
            temp_path = f.name
        
        try:
            with patch('backend.plugin_signer.TRUSTED_KEYS_FILE', temp_path):
                result = has_trusted_keys()
                assert result is True
        finally:
            os.unlink(temp_path)


class TestSignatureVerificationEnabled:
    """Test signature verification enabled detection."""
    
    @patch('backend.plugin_signer.is_gpg_available')
    @patch('backend.plugin_signer.has_trusted_keys')
    def test_enabled_when_prerequisites_met(self, mock_has_keys, mock_gpg):
        """Test verification enabled when GPG and keys are available."""
        mock_gpg.return_value = True
        mock_has_keys.return_value = True
        
        # Mock database to return no explicit setting
        with patch('models.db.db') as mock_db:
            mock_db.get_setting.return_value = None
            result = is_signature_verification_enabled()
            assert result is True
    
    @patch('backend.plugin_signer.is_gpg_available')
    @patch('backend.plugin_signer.has_trusted_keys')
    def test_disabled_when_gpg_missing(self, mock_has_keys, mock_gpg):
        """Test verification disabled when GPG is not available."""
        mock_gpg.return_value = False
        mock_has_keys.return_value = True
        
        result = is_signature_verification_enabled()
        assert result is False
    
    @patch('backend.plugin_signer.is_gpg_available')
    @patch('backend.plugin_signer.has_trusted_keys')
    def test_disabled_when_keys_missing(self, mock_has_keys, mock_gpg):
        """Test verification disabled when trusted keys are missing."""
        mock_gpg.return_value = True
        mock_has_keys.return_value = False
        
        result = is_signature_verification_enabled()
        assert result is False
    
    @patch('backend.plugin_signer.is_gpg_available')
    @patch('backend.plugin_signer.has_trusted_keys')
    def test_disabled_by_env_variable(self, mock_has_keys, mock_gpg):
        """Test verification disabled by environment variable."""
        mock_gpg.return_value = True
        mock_has_keys.return_value = True
        
        with patch.dict(os.environ, {'PLUGIN_SIGNATURE_REQUIRED': 'false'}):
            result = is_signature_verification_enabled()
            assert result is False
    
    @patch('backend.plugin_signer.is_gpg_available')
    @patch('backend.plugin_signer.has_trusted_keys')
    def test_disabled_by_database_setting(self, mock_has_keys, mock_gpg):
        """Test verification disabled by database setting."""
        mock_gpg.return_value = True
        mock_has_keys.return_value = True
        
        with patch('models.db.db') as mock_db:
            mock_db.get_setting.return_value = '0'
            result = is_signature_verification_enabled()
            assert result is False


class TestVerifyPluginSignature:
    """Test plugin signature verification."""
    
    def test_verify_when_disabled(self):
        """Test verification passes when disabled."""
        with patch('backend.plugin_signer.is_signature_verification_enabled', return_value=False):
            success, msg = verify_plugin_signature('/fake/path.zip')
            assert success is True
            assert 'disabled' in msg.lower()
    
    @patch('backend.plugin_signer.is_signature_verification_enabled')
    @patch('backend.plugin_signer.is_gpg_available')
    def test_verify_fails_when_gpg_missing(self, mock_gpg, mock_enabled):
        """Test verification fails when GPG is not available."""
        mock_enabled.return_value = True
        mock_gpg.return_value = False
        
        success, msg = verify_plugin_signature('/fake/path.zip')
        assert success is False
        assert 'GPG not available' in msg
    
    @patch('backend.plugin_signer.is_signature_verification_enabled')
    @patch('backend.plugin_signer.is_gpg_available')
    @patch('backend.plugin_signer.has_trusted_keys')
    def test_verify_fails_when_keys_missing(self, mock_keys, mock_gpg, mock_enabled):
        """Test verification fails when trusted keys are missing."""
        mock_enabled.return_value = True
        mock_gpg.return_value = True
        mock_keys.return_value = False
        
        success, msg = verify_plugin_signature('/fake/path.zip')
        assert success is False
        assert 'No trusted keys' in msg
    
    @patch('backend.plugin_signer.is_signature_verification_enabled')
    @patch('backend.plugin_signer.is_gpg_available')
    @patch('backend.plugin_signer.has_trusted_keys')
    def test_verify_fails_when_signature_missing(self, mock_keys, mock_gpg, mock_enabled):
        """Test verification fails when signature file is missing."""
        mock_enabled.return_value = True
        mock_gpg.return_value = True
        mock_keys.return_value = True
        
        with tempfile.NamedTemporaryFile(suffix='.zip', delete=False) as f:
            zip_path = f.name
        
        try:
            success, msg = verify_plugin_signature(zip_path)
            assert success is False
            assert 'No signature file' in msg
        finally:
            os.unlink(zip_path)
    
    @patch('backend.plugin_signer.is_signature_verification_enabled')
    @patch('backend.plugin_signer.is_gpg_available')
    @patch('backend.plugin_signer.has_trusted_keys')
    @patch('subprocess.run')
    def test_verify_success_with_good_signature(self, mock_run, mock_keys, mock_gpg, mock_enabled):
        """Test verification succeeds with valid signature."""
        mock_enabled.return_value = True
        mock_gpg.return_value = True
        mock_keys.return_value = True
        
        # Mock GPG import and verify commands
        mock_import = MagicMock()
        mock_import.returncode = 0
        mock_import.stderr = b''
        
        mock_verify = MagicMock()
        mock_verify.returncode = 0
        mock_verify.stdout = b'[GNUPG:] GOODSIG ABC123 Test Signer <test@example.com>'
        mock_verify.stderr = b''
        
        mock_run.side_effect = [mock_import, mock_verify]
        
        with tempfile.NamedTemporaryFile(suffix='.zip', delete=False) as f:
            zip_path = f.name
        
        with tempfile.NamedTemporaryFile(suffix='.sig', delete=False) as f:
            sig_path = f.name
        
        try:
            success, msg = verify_plugin_signature(zip_path, sig_path)
            assert success is True
            assert 'Signature valid' in msg
        finally:
            os.unlink(zip_path)
            os.unlink(sig_path)
    
    @patch('backend.plugin_signer.is_signature_verification_enabled')
    @patch('backend.plugin_signer.is_gpg_available')
    @patch('backend.plugin_signer.has_trusted_keys')
    @patch('subprocess.run')
    def test_verify_fails_with_bad_signature(self, mock_run, mock_keys, mock_gpg, mock_enabled):
        """Test verification fails with invalid signature."""
        mock_enabled.return_value = True
        mock_gpg.return_value = True
        mock_keys.return_value = True
        
        # Mock GPG import success but verify failure
        mock_import = MagicMock()
        mock_import.returncode = 0
        mock_import.stderr = b''
        
        mock_verify = MagicMock()
        mock_verify.returncode = 1
        mock_verify.stdout = b''
        mock_verify.stderr = b'gpg: BAD signature from "Test Signer <test@example.com>"'
        
        mock_run.side_effect = [mock_import, mock_verify]
        
        with tempfile.NamedTemporaryFile(suffix='.zip', delete=False) as f:
            zip_path = f.name
        
        with tempfile.NamedTemporaryFile(suffix='.sig', delete=False) as f:
            sig_path = f.name
        
        try:
            success, msg = verify_plugin_signature(zip_path, sig_path)
            assert success is False
            assert 'verification failed' in msg.lower()
        finally:
            os.unlink(zip_path)
            os.unlink(sig_path)


class TestGetSignatureStatus:
    """Test signature status reporting."""
    
    @patch('backend.plugin_signer.is_signature_verification_enabled')
    @patch('backend.plugin_signer.is_gpg_available')
    @patch('backend.plugin_signer.has_trusted_keys')
    def test_get_status(self, mock_keys, mock_gpg, mock_enabled):
        """Test getting signature verification status."""
        mock_enabled.return_value = True
        mock_gpg.return_value = True
        mock_keys.return_value = True
        
        status = get_signature_status()
        
        assert isinstance(status, dict)
        assert 'enabled' in status
        assert 'gpg_available' in status
        assert 'has_trusted_keys' in status
        assert 'trusted_keys_path' in status
        assert 'key_count' in status
        
        assert status['enabled'] is True
        assert status['gpg_available'] is True
        assert status['has_trusted_keys'] is True


if __name__ == '__main__':
    pytest.main([__file__, '-v'])

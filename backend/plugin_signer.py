"""
Plugin Code Signing — cryptographic signature verification for plugins.

Provides GPG-based signature verification to ensure plugins are from trusted
sources and haven't been tampered with. This prevents supply chain attacks
and malicious plugin installation.

Signature Format:
- Plugins must include a `plugin.sig` file containing a detached GPG signature
- The signature covers the entire plugin.zip file (excluding the signature itself)
- Signatures are verified against a trusted keyring

Setup:
1. Generate a GPG key pair for plugin signing:
   gpg --gen-key

2. Export the public key to the trusted keyring:
   gpg --export --armor <key-id> > plugins/trusted_keys.asc

3. Sign a plugin:
   gpg --detach-sign --armor -o plugin.sig plugin.zip

4. Include plugin.sig in the plugin distribution
"""

import os
import subprocess
import tempfile
import logging
from typing import Tuple, Optional

_logger = logging.getLogger(__name__)

BASE_DIR = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
PLUGINS_DIR = os.path.join(BASE_DIR, 'plugins')
TRUSTED_KEYS_FILE = os.path.join(PLUGINS_DIR, 'trusted_keys.asc')


class SignatureVerificationError(Exception):
    """Raised when signature verification fails."""
    pass


def is_gpg_available() -> bool:
    """
    Check if GPG is available on the system.
    
    Returns:
        bool: True if gpg command is available, False otherwise
    """
    try:
        result = subprocess.run(
            ['gpg', '--version'],
            capture_output=True,
            timeout=5,
            check=False
        )
        return result.returncode == 0
    except (FileNotFoundError, subprocess.TimeoutExpired):
        return False


def has_trusted_keys() -> bool:
    """
    Check if trusted keys file exists.
    
    Returns:
        bool: True if trusted_keys.asc exists and is non-empty
    """
    if not os.path.isfile(TRUSTED_KEYS_FILE):
        return False
    
    try:
        stat = os.stat(TRUSTED_KEYS_FILE)
        return stat.st_size > 0
    except OSError:
        return False


def is_signature_verification_enabled() -> bool:
    """
    Check if signature verification is enabled.
    
    Verification is enabled when:
    1. GPG is available on the system
    2. Trusted keys file exists
    3. PLUGIN_SIGNATURE_REQUIRED setting is not explicitly disabled
    
    Returns:
        bool: True if verification should be enforced
    """
    # Check if explicitly disabled via environment variable
    import os as env_os
    if env_os.environ.get('PLUGIN_SIGNATURE_REQUIRED', '').lower() == 'false':
        return False
    
    # Check database setting
    try:
        from models.db import db
        setting = db.get_setting('plugin_signature_required')
        if setting == '0':
            return False
        elif setting == '1':
            # Explicitly enabled, check prerequisites
            if not is_gpg_available():
                _logger.warning("Plugin signature verification enabled but GPG not available")
                return False
            if not has_trusted_keys():
                _logger.warning("Plugin signature verification enabled but no trusted keys found")
                return False
            return True
    except Exception as e:
        _logger.debug("Could not check plugin_signature_required setting: %s", e)
    
    # Default: enable if GPG and keys are available
    return is_gpg_available() and has_trusted_keys()


def verify_plugin_signature(plugin_zip_path: str, signature_path: Optional[str] = None) -> Tuple[bool, str]:
    """
    Verify the GPG signature of a plugin zip file.
    
    Args:
        plugin_zip_path: Path to the plugin zip file
        signature_path: Optional path to signature file. If None, looks for
                       plugin_zip_path + '.sig' or plugin_zip_path + '.asc'
    
    Returns:
        Tuple of (success: bool, message: str)
        - (True, "Signature valid") if verification succeeds
        - (False, error_message) if verification fails
    
    Raises:
        SignatureVerificationError: If verification is required but fails
    """
    # Check if verification is enabled
    if not is_signature_verification_enabled():
        _logger.debug("Plugin signature verification is disabled")
        return True, "Signature verification disabled"
    
    # Check if GPG is available
    if not is_gpg_available():
        msg = "GPG not available for signature verification"
        _logger.error(msg)
        return False, msg
    
    # Check if trusted keys exist
    if not has_trusted_keys():
        msg = f"No trusted keys found at {TRUSTED_KEYS_FILE}"
        _logger.error(msg)
        return False, msg
    
    # Find signature file
    if signature_path is None:
        # Try common signature file extensions
        for ext in ['.sig', '.asc']:
            candidate = plugin_zip_path + ext
            if os.path.isfile(candidate):
                signature_path = candidate
                break
    
    if signature_path is None or not os.path.isfile(signature_path):
        msg = f"No signature file found for {plugin_zip_path}"
        _logger.error(msg)
        return False, msg
    
    # Create temporary GPG home directory for isolated verification
    with tempfile.TemporaryDirectory(prefix='gpg_verify_') as temp_gnupg:
        try:
            # Import trusted keys into temporary keyring
            import_result = subprocess.run(
                [
                    'gpg',
                    '--homedir', temp_gnupg,
                    '--batch',
                    '--yes',
                    '--import',
                    TRUSTED_KEYS_FILE
                ],
                capture_output=True,
                timeout=30,
                check=False
            )
            
            if import_result.returncode != 0:
                msg = f"Failed to import trusted keys: {import_result.stderr.decode('utf-8', errors='replace')}"
                _logger.error(msg)
                return False, msg
            
            # Verify signature
            verify_result = subprocess.run(
                [
                    'gpg',
                    '--homedir', temp_gnupg,
                    '--batch',
                    '--status-fd', '1',
                    '--verify',
                    signature_path,
                    plugin_zip_path
                ],
                capture_output=True,
                timeout=30,
                check=False
            )
            
            stdout = verify_result.stdout.decode('utf-8', errors='replace')
            stderr = verify_result.stderr.decode('utf-8', errors='replace')
            
            # Check for GOODSIG in status output
            if verify_result.returncode == 0 and '[GNUPG:] GOODSIG' in stdout:
                # Extract signer info
                signer = "unknown"
                for line in stdout.split('\n'):
                    if '[GNUPG:] GOODSIG' in line:
                        parts = line.split('[GNUPG:] GOODSIG', 1)
                        if len(parts) > 1:
                            signer = parts[1].strip()
                        break
                
                msg = f"Signature valid (signed by: {signer})"
                _logger.info(msg)
                return True, msg
            else:
                # Signature verification failed
                msg = f"Signature verification failed: {stderr}"
                _logger.error(msg)
                return False, msg
        
        except subprocess.TimeoutExpired:
            msg = "GPG verification timed out"
            _logger.error(msg)
            return False, msg
        
        except Exception as e:
            msg = f"Signature verification error: {e}"
            _logger.error(msg, exc_info=True)
            return False, msg


def create_plugin_signature(plugin_zip_path: str, key_id: Optional[str] = None, output_path: Optional[str] = None) -> Tuple[bool, str]:
    """
    Create a detached GPG signature for a plugin zip file.
    
    This is a helper function for plugin developers to sign their plugins.
    
    Args:
        plugin_zip_path: Path to the plugin zip file to sign
        key_id: Optional GPG key ID to use for signing. If None, uses default key
        output_path: Optional output path for signature. If None, uses plugin_zip_path + '.sig'
    
    Returns:
        Tuple of (success: bool, message: str)
    """
    if not is_gpg_available():
        return False, "GPG not available"
    
    if not os.path.isfile(plugin_zip_path):
        return False, f"Plugin file not found: {plugin_zip_path}"
    
    if output_path is None:
        output_path = plugin_zip_path + '.sig'
    
    try:
        cmd = ['gpg', '--detach-sign', '--armor', '-o', output_path]
        
        if key_id:
            cmd.extend(['--local-user', key_id])
        
        cmd.append(plugin_zip_path)
        
        result = subprocess.run(
            cmd,
            capture_output=True,
            timeout=60,
            check=False
        )
        
        if result.returncode == 0:
            msg = f"Signature created: {output_path}"
            _logger.info(msg)
            return True, msg
        else:
            msg = f"Failed to create signature: {result.stderr.decode('utf-8', errors='replace')}"
            _logger.error(msg)
            return False, msg
    
    except subprocess.TimeoutExpired:
        return False, "GPG signing timed out"
    
    except Exception as e:
        msg = f"Signature creation error: {e}"
        _logger.error(msg, exc_info=True)
        return False, msg


def get_signature_status() -> dict:
    """
    Get the current status of signature verification system.
    
    Returns:
        dict with keys:
        - enabled: bool, whether verification is enabled
        - gpg_available: bool, whether GPG is installed
        - has_trusted_keys: bool, whether trusted keys are configured
        - trusted_keys_path: str, path to trusted keys file
        - key_count: int, number of trusted keys (if available)
    """
    status = {
        'enabled': is_signature_verification_enabled(),
        'gpg_available': is_gpg_available(),
        'has_trusted_keys': has_trusted_keys(),
        'trusted_keys_path': TRUSTED_KEYS_FILE,
        'key_count': 0
    }
    
    # Try to count keys in trusted keyring
    if status['gpg_available'] and status['has_trusted_keys']:
        try:
            with tempfile.TemporaryDirectory(prefix='gpg_status_') as temp_gnupg:
                # Import keys
                subprocess.run(
                    ['gpg', '--homedir', temp_gnupg, '--batch', '--yes', '--import', TRUSTED_KEYS_FILE],
                    capture_output=True,
                    timeout=10,
                    check=False
                )
                
                # List keys
                result = subprocess.run(
                    ['gpg', '--homedir', temp_gnupg, '--batch', '--list-keys', '--with-colons'],
                    capture_output=True,
                    timeout=10,
                    check=False
                )
                
                if result.returncode == 0:
                    # Count 'pub' lines (public keys)
                    output = result.stdout.decode('utf-8', errors='replace')
                    status['key_count'] = output.count('\npub:')
        
        except Exception as e:
            _logger.debug("Could not count trusted keys: %s", e)
    
    return status

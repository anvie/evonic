import sqlite3
import uuid
from typing import Dict, Any, List, Optional


class WorkplaceMixin:
    """Workplace CRUD and tunnel connector management. Requires self._connect() from the host class."""

    # ==================== Workplaces ====================

    def get_workplaces(self) -> List[Dict[str, Any]]:
        with self._connect() as conn:
            conn.row_factory = sqlite3.Row
            cursor = conn.cursor()
            cursor.execute("SELECT id, name, type, config, status, error_msg, last_connected_at, created_at, updated_at FROM workplaces ORDER BY name")
            return [dict(row) for row in cursor.fetchall()]

    def get_workplace(self, workplace_id: str) -> Optional[Dict[str, Any]]:
        with self._connect() as conn:
            conn.row_factory = sqlite3.Row
            cursor = conn.cursor()
            cursor.execute("SELECT id, name, type, config, status, error_msg, last_connected_at, created_at, updated_at FROM workplaces WHERE id = ?", (workplace_id,))
            row = cursor.fetchone()
            return dict(row) if row else None

    def create_workplace(self, data: Dict[str, Any]) -> str:
        workplace_id = data.get('id') or uuid.uuid4().hex[:12]
        import json
        config = data.get('config', {})
        if isinstance(config, dict):
            config = json.dumps(config)
        with self._connect() as conn:
            cursor = conn.cursor()
            cursor.execute("""
                INSERT INTO workplaces (id, name, type, config, status)
                VALUES (?, ?, ?, ?, 'disconnected')
            """, (workplace_id, data['name'], data['type'], config))
            conn.commit()
        return workplace_id

    def update_workplace(self, workplace_id: str, data: Dict[str, Any]) -> bool:
        import json
        allowed = {'name', 'config', 'status', 'error_msg', 'last_connected_at'}
        updates = {k: v for k, v in data.items() if k in allowed}
        if 'config' in updates and isinstance(updates['config'], dict):
            updates['config'] = json.dumps(updates['config'])
        if not updates:
            return False
        set_clause = ", ".join(f"{k} = ?" for k in updates)
        values = list(updates.values()) + [workplace_id]
        with self._connect() as conn:
            cursor = conn.cursor()
            cursor.execute(
                f"UPDATE workplaces SET {set_clause}, updated_at = CURRENT_TIMESTAMP WHERE id = ?",
                values
            )
            conn.commit()
            return cursor.rowcount > 0

    def delete_workplace(self, workplace_id: str) -> bool:
        with self._connect() as conn:
            cursor = conn.cursor()
            cursor.execute("DELETE FROM workplaces WHERE id = ?", (workplace_id,))
            conn.commit()
            return cursor.rowcount > 0

    def clone_workplace(self, source_id: str, new_name: str) -> Optional[str]:
        """Clone a workplace: copy type and config, fresh id, disconnected status.

        Tunnel connectors are NOT copied — the clone needs its own pairing.
        Returns the new workplace ID, or None if source not found.
        """
        source = self.get_workplace(source_id)
        if not source:
            return None
        new_id = uuid.uuid4().hex[:12]
        with self._connect() as conn:
            cursor = conn.cursor()
            cursor.execute("""
                INSERT INTO workplaces (id, name, type, config, status)
                VALUES (?, ?, ?, ?, 'disconnected')
            """, (new_id, new_name, source['type'], source['config']))
            conn.commit()
        return new_id

    def get_workplace_agents(self, workplace_id: str) -> List[Dict[str, Any]]:
        with self._connect() as conn:
            conn.row_factory = sqlite3.Row
            cursor = conn.cursor()
            cursor.execute(
                "SELECT id, name, description, enabled, avatar_path FROM agents WHERE workplace_id = ? ORDER BY name",
                (workplace_id,)
            )
            return [dict(row) for row in cursor.fetchall()]

    def update_workplace_status(self, workplace_id: str, status: str, error_msg: Optional[str] = None) -> None:
        import datetime
        last_connected = None
        if status == 'connected':
            last_connected = datetime.datetime.utcnow().strftime('%Y-%m-%dT%H:%M:%SZ')
        with self._connect() as conn:
            cursor = conn.cursor()
            if last_connected:
                cursor.execute("""
                    UPDATE workplaces SET status = ?, error_msg = ?, last_connected_at = ?,
                    updated_at = CURRENT_TIMESTAMP WHERE id = ?
                """, (status, error_msg, last_connected, workplace_id))
            else:
                cursor.execute("""
                    UPDATE workplaces SET status = ?, error_msg = ?,
                    updated_at = CURRENT_TIMESTAMP WHERE id = ?
                """, (status, error_msg, workplace_id))
            conn.commit()

    # ==================== Tunnel Connectors ====================

    def get_connector(self, connector_id: str) -> Optional[Dict[str, Any]]:
        with self._connect() as conn:
            conn.row_factory = sqlite3.Row
            cursor = conn.cursor()
            cursor.execute("SELECT id, workplace_id, connector_token, pairing_code, pairing_expires_at, device_name, platform, version, last_seen_at FROM tunnel_connectors WHERE id = ?", (connector_id,))
            row = cursor.fetchone()
            return dict(row) if row else None

    def get_connector_by_token(self, token: str) -> Optional[Dict[str, Any]]:
        with self._connect() as conn:
            conn.row_factory = sqlite3.Row
            cursor = conn.cursor()
            cursor.execute("SELECT id, workplace_id, connector_token, pairing_code, pairing_expires_at, device_name, platform, version, last_seen_at FROM tunnel_connectors WHERE connector_token = ?", (token,))
            row = cursor.fetchone()
            return dict(row) if row else None

    def get_connector_by_workplace(self, workplace_id: str) -> Optional[Dict[str, Any]]:
        with self._connect() as conn:
            conn.row_factory = sqlite3.Row
            cursor = conn.cursor()
            cursor.execute("SELECT id, workplace_id, connector_token, pairing_code, pairing_expires_at, device_name, platform, version, last_seen_at FROM tunnel_connectors WHERE workplace_id = ?", (workplace_id,))
            row = cursor.fetchone()
            return dict(row) if row else None

    def get_connector_by_pairing_code(self, code: str) -> Optional[Dict[str, Any]]:
        with self._connect() as conn:
            conn.row_factory = sqlite3.Row
            cursor = conn.cursor()
            cursor.execute("SELECT id, workplace_id, connector_token, pairing_code, pairing_expires_at, device_name, platform, version, last_seen_at FROM tunnel_connectors WHERE pairing_code = ?", (code,))
            row = cursor.fetchone()
            return dict(row) if row else None

    def create_connector(self, data: Dict[str, Any]) -> str:
        connector_id = data.get('id') or uuid.uuid4().hex
        with self._connect() as conn:
            cursor = conn.cursor()
            cursor.execute("""
                INSERT INTO tunnel_connectors
                (id, workplace_id, connector_token, pairing_code, pairing_expires_at, device_name, platform, version)
                VALUES (?, ?, ?, ?, ?, ?, ?, ?)
            """, (
                connector_id,
                data['workplace_id'],
                data['connector_token'],
                data.get('pairing_code'),
                data.get('pairing_expires_at'),
                data.get('device_name'),
                data.get('platform'),
                data.get('version'),
            ))
            conn.commit()
        return connector_id

    def update_connector(self, connector_id: str, data: Dict[str, Any]) -> bool:
        allowed = {'connector_token', 'pairing_code', 'pairing_expires_at',
                   'device_name', 'platform', 'version', 'last_seen_at'}
        updates = {k: v for k, v in data.items() if k in allowed}
        if not updates:
            return False
        set_clause = ", ".join(f"{k} = ?" for k in updates)
        values = list(updates.values()) + [connector_id]
        with self._connect() as conn:
            cursor = conn.cursor()
            cursor.execute(f"UPDATE tunnel_connectors SET {set_clause} WHERE id = ?", values)
            conn.commit()
            return cursor.rowcount > 0

    def delete_connector(self, connector_id: str) -> bool:
        with self._connect() as conn:
            cursor = conn.cursor()
            cursor.execute("DELETE FROM tunnel_connectors WHERE id = ?", (connector_id,))
            conn.commit()
            return cursor.rowcount > 0

    def set_pairing_code(self, workplace_id: str, code: str, expires_at: str) -> str:
        """Create or update pairing code for a tunnel workplace. Returns connector_id."""
        existing = self.get_connector_by_workplace(workplace_id)
        if existing:
            self.update_connector(existing['id'], {
                'pairing_code': code,
                'pairing_expires_at': expires_at,
            })
            return existing['id']
        connector_id = uuid.uuid4().hex
        # connector_token is NULL until Evonet completes pairing
        self.create_connector({
            'id': connector_id,
            'workplace_id': workplace_id,
            'connector_token': None,
            'pairing_code': code,
            'pairing_expires_at': expires_at,
        })
        return connector_id

    def clear_pairing_code(self, workplace_id: str) -> None:
        existing = self.get_connector_by_workplace(workplace_id)
        if existing:
            self.update_connector(existing['id'], {
                'pairing_code': None,
                'pairing_expires_at': None,
            })

    def finalize_pairing(self, pairing_code: str, connector_token: str,
                         device_name: str, platform: str, version: str) -> Optional[Dict[str, Any]]:
        """Complete pairing: validate code, assign token, return connector record. Returns None if code invalid/expired."""
        import datetime
        connector = self.get_connector_by_pairing_code(pairing_code)
        if not connector:
            return None
        expires_at = connector.get('pairing_expires_at')
        if expires_at:
            try:
                exp = datetime.datetime.fromisoformat(expires_at.replace('Z', '+00:00'))
                now = datetime.datetime.now(datetime.timezone.utc)
                if now > exp:
                    return None
            except Exception:
                return None
        # Preserve existing token — it acts as a permanent master token for this workplace.
        # A new token is only assigned if the connector has never been paired before.
        token_to_use = connector.get('connector_token') or connector_token
        self.update_connector(connector['id'], {
            'connector_token': token_to_use,
            'pairing_code': None,
            'pairing_expires_at': None,
            'device_name': device_name,
            'platform': platform,
            'version': version,
        })
        return self.get_connector(connector['id'])

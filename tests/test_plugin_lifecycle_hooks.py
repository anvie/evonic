import json
import textwrap

from backend import plugin_lifecycle


def _plugin(tmp_path, name, handler_source):
    plugin_dir = tmp_path / name
    plugin_dir.mkdir()
    (plugin_dir / "plugin.json").write_text(json.dumps({"id": name, "enabled": True}))
    (plugin_dir / "handler.py").write_text(textwrap.dedent(handler_source))


def _manager(monkeypatch, tmp_path):
    monkeypatch.setattr(plugin_lifecycle, "PLUGINS_DIR", str(tmp_path))
    monkeypatch.setattr(plugin_lifecycle.PluginManager, "_load_all", lambda self: None)
    return plugin_lifecycle.PluginManager()


def test_lifecycle_hooks_are_called_on_load_and_unload(monkeypatch, tmp_path):
    _plugin(
        tmp_path,
        "lifecycle_test",
        """
        calls = []
        def on_enable():
            calls.append("enabled")
        def on_disable():
            calls.append("disabled")
        """,
    )
    manager = _manager(monkeypatch, tmp_path)

    manager._load_plugin("lifecycle_test")
    module = manager._modules["lifecycle_test"]
    assert module.calls == ["enabled"]

    manager._unload_plugin("lifecycle_test")
    assert module.calls == ["enabled", "disabled"]


def test_lifecycle_hook_can_accept_sdk(monkeypatch, tmp_path):
    _plugin(
        tmp_path,
        "sdk_lifecycle_test",
        """
        received = None
        def on_enable(sdk):
            global received
            received = sdk.plugin_id
        """,
    )
    manager = _manager(monkeypatch, tmp_path)

    manager._load_plugin("sdk_lifecycle_test")

    assert manager._modules["sdk_lifecycle_test"].received == "sdk_lifecycle_test"

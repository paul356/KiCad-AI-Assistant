"""Tests for PluginSettings: load, save, defaults, and round-trip."""
import json
import os
import sys
import tempfile

import pytest

from kicad_plugin.settings import PluginSettings


@pytest.fixture()
def tmp_config_dir(tmp_path):
    return str(tmp_path)


class TestPluginSettingsDefaults:
    def test_default_provider(self):
        s = PluginSettings()
        assert s.llm_provider == "openai"

    def test_default_model(self):
        s = PluginSettings()
        assert s.llm_model == "gpt-4o"

    def test_default_port_is_zero(self):
        s = PluginSettings()
        assert s.server_port == 0

    def test_default_show_tool_log(self):
        s = PluginSettings()
        assert s.show_tool_log is True

    def test_resolved_log_dir_falls_back_to_config_dir(self, tmp_config_dir):
        s = PluginSettings(config_dir=tmp_config_dir)
        assert s.resolved_log_dir == tmp_config_dir

    def test_resolved_log_dir_custom(self, tmp_config_dir, tmp_path):
        custom = str(tmp_path / "logs")
        s = PluginSettings(config_dir=tmp_config_dir, server_log_dir=custom)
        assert s.resolved_log_dir == custom


class TestPluginSettingsSaveLoad:
    def test_save_creates_file(self, tmp_config_dir):
        s = PluginSettings(config_dir=tmp_config_dir)
        s.save()
        assert os.path.exists(s.settings_path)

    def test_round_trip(self, tmp_config_dir):
        s = PluginSettings(
            config_dir=tmp_config_dir,
            llm_provider="anthropic",
            llm_api_key="sk-test",
            llm_model="claude-opus-4-5",
            server_port=8765,
            show_tool_log=False,
        )
        s.save()

        loaded = PluginSettings.load(config_dir=tmp_config_dir)
        assert loaded.llm_provider == "anthropic"
        assert loaded.llm_api_key == "sk-test"
        assert loaded.llm_model == "claude-opus-4-5"
        assert loaded.server_port == 8765
        assert loaded.show_tool_log is False

    def test_load_missing_file_returns_defaults(self, tmp_config_dir):
        loaded = PluginSettings.load(config_dir=tmp_config_dir)
        assert loaded.llm_provider == "openai"

    def test_load_corrupt_file_returns_defaults(self, tmp_config_dir):
        s = PluginSettings(config_dir=tmp_config_dir)
        os.makedirs(tmp_config_dir, exist_ok=True)
        with open(s.settings_path, "w") as f:
            f.write("not valid json {{{")
        loaded = PluginSettings.load(config_dir=tmp_config_dir)
        assert loaded.llm_provider == "openai"

    def test_api_key_not_in_env(self, tmp_config_dir):
        """API key must be stored in settings file, not leaked to environment."""
        s = PluginSettings(config_dir=tmp_config_dir, llm_api_key="sk-secret")
        s.save()
        assert "sk-secret" not in os.environ.get("OPENAI_API_KEY", "")
        assert "sk-secret" not in os.environ.get("ANTHROPIC_API_KEY", "")

    def test_config_dir_not_written_to_file(self, tmp_config_dir):
        s = PluginSettings(config_dir=tmp_config_dir)
        s.save()
        with open(s.settings_path) as f:
            data = json.load(f)
        assert "config_dir" not in data


class TestPluginSettingsSecurity:
    def test_api_key_not_in_repr(self):
        s = PluginSettings(llm_api_key="sk-super-secret")
        r = repr(s)
        assert "sk-super-secret" not in r

    @pytest.mark.skipif(sys.platform == "win32", reason="Unix permissions only")
    def test_save_creates_file_with_owner_only_permissions(self, tmp_path):
        s = PluginSettings(config_dir=str(tmp_path), llm_api_key="sk-secret")
        s.save()
        mode = oct(os.stat(s.settings_path).st_mode)[-3:]
        assert mode == "600", f"Expected 600, got {mode}"

    def test_load_ignores_unknown_keys(self, tmp_path):
        s = PluginSettings(config_dir=str(tmp_path))
        os.makedirs(str(tmp_path), exist_ok=True)
        with open(s.settings_path, "w") as f:
            json.dump({"llm_provider": "anthropic", "unknown_future_key": "ignored"}, f)
        loaded = PluginSettings.load(config_dir=str(tmp_path))
        assert loaded.llm_provider == "anthropic"  # known key applied

    def test_load_wrong_type_field_accepted_as_is(self, tmp_path):
        s = PluginSettings(config_dir=str(tmp_path))
        os.makedirs(str(tmp_path), exist_ok=True)
        with open(s.settings_path, "w") as f:
            json.dump({"server_port": "not_a_number"}, f)
        # Current behaviour: value is set without type validation
        loaded = PluginSettings.load(config_dir=str(tmp_path))
        assert loaded.server_port == "not_a_number"


"""
Plugin settings: load/save from KiCad user config directory.
"""
from __future__ import annotations

import json
import logging
import os
import platform
from dataclasses import dataclass, field, asdict
from typing import Optional

log = logging.getLogger(__name__)

_SETTINGS_FILENAME = "kicad_ai_assistant.json"


def _default_config_dir() -> str:
    """Return the KiCad user config directory for the current platform."""
    system = platform.system()
    if system == "Darwin":
        base = os.path.expanduser("~/Library/Preferences/kicad")
    elif system == "Windows":
        base = os.path.join(os.environ.get("APPDATA", os.path.expanduser("~")), "kicad")
    else:  # Linux and others
        base = os.path.expanduser("~/.config/kicad")
    return base


@dataclass
class PluginSettings:
    """All user-configurable settings for the KiCad AI Assistant plugin."""

    # LLM provider
    llm_provider: str = "openai"          # "openai" | "anthropic" | "custom"
    llm_api_key: str = field(default="", repr=False)  # never leak key in logs/repr
    llm_model: str = "gpt-4o"            # model name
    llm_base_url: str = ""               # custom endpoint URL (if provider == "custom")

    # MCP server
    server_port: int = 0                  # 0 = auto-select a free port at startup
    server_log_dir: str = ""              # "" = KiCad user config dir
    python_executable: str = ""           # "" = auto-detect (shutil.which("python3"))

    # UI preferences
    show_tool_log: bool = True            # show the tool-call log by default

    # Internal — not shown in settings UI
    config_dir: str = field(default_factory=_default_config_dir, repr=False)

    # ------------------------------------------------------------------ #

    @property
    def settings_path(self) -> str:
        return os.path.join(self.config_dir, _SETTINGS_FILENAME)

    @property
    def resolved_log_dir(self) -> str:
        return self.server_log_dir or self.config_dir

    def save(self) -> None:
        """Persist settings to disk with owner-only permissions (0o600)."""
        os.makedirs(self.config_dir, exist_ok=True)
        data = {k: v for k, v in asdict(self).items() if k != "config_dir"}
        try:
            # Write with explicit 0o600 so the API key is not world-readable
            fd = os.open(self.settings_path, os.O_WRONLY | os.O_CREAT | os.O_TRUNC, 0o600)
            with os.fdopen(fd, "w", encoding="utf-8") as f:
                json.dump(data, f, indent=2)
            log.debug("Settings saved to %s", self.settings_path)
        except OSError as e:
            log.error("Failed to save settings: %s", e)

    @classmethod
    def load(cls, config_dir: Optional[str] = None) -> "PluginSettings":
        """Load settings from disk, returning defaults if the file doesn't exist."""
        inst = cls()
        if config_dir:
            inst.config_dir = config_dir

        if not os.path.exists(inst.settings_path):
            log.debug("No settings file found; using defaults")
            return inst

        try:
            with open(inst.settings_path, "r", encoding="utf-8") as f:
                data = json.load(f)
            for key, value in data.items():
                if hasattr(inst, key) and key != "config_dir":
                    setattr(inst, key, value)
            log.debug(f"Settings loaded from {inst.settings_path}")
        except (OSError, json.JSONDecodeError) as e:
            log.warning(f"Could not load settings ({e}); using defaults")

        return inst

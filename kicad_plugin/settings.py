"""
Plugin settings: load/save from KiCad user config directory.
"""

from __future__ import annotations

from dataclasses import asdict, dataclass, field
import json
import logging
import os
import platform
import re

log = logging.getLogger(__name__)

_SETTINGS_FILENAME = "kicad_ai_assistant.json"


def _detect_kicad_version() -> str | None:
    """Detect KiCad version for the plugin context.

    Tries in order:
    1. KICAD_VERSION environment variable
    2. Plugin directory path (e.g. .../kicad/10.0/scripting/plugins/...)
    3. KICAD{N}_* variables (e.g. KICAD10_SYMBOL_DIR → "10.0")
    Returns None if no source yields a version.
    """
    # 1. From KICAD_VERSION environment variable
    ver = os.environ.get("KICAD_VERSION")
    if ver:
        return ver

    # 2. From the plugin directory path
    try:
        path = os.path.abspath(__file__)
        m = re.search(r"[/\\]kicad[/\\](\d+\.\d+)[/\\]", path, re.IGNORECASE)
        if m:
            return m.group(1)
    except Exception as e:
        log.debug("Could not detect KiCad version from path: %s", e)

    # 3. From KICAD{N}_* environment variables
    for key in os.environ:
        if key.startswith("KICAD") and "_" in key:
            major = key[5:].split("_")[0]
            if major.isdigit():
                return f"{major}.0"

    return None


def _get_kcaa_data_dir() -> str:
    """Return the kcaa data directory under the KiCad user config directory.

    KiCad version is detected from KICAD{N}_* environment variables or plugin path.
    """
    kicad_version = _detect_kicad_version()
    if kicad_version is None:
        raise RuntimeError(
            "Cannot detect KiCad version. Ensure KICAD{N}_* environment variables "
            "are set (e.g. KICAD10_SYMBOL_DIR) or the plugin is installed under "
            "a versioned KiCad directory (e.g. .../kicad/10.0/scripting/plugins/...)."
        )
    system = platform.system()
    if system == "Darwin":
        base = os.path.expanduser(f"~/Library/Preferences/kicad/{kicad_version}")
    elif system == "Windows":
        base = os.path.join(
            os.environ.get("APPDATA", os.path.expanduser("~")), "kicad", kicad_version
        )
    else:  # Linux and others
        base = os.path.expanduser(f"~/.config/kicad/{kicad_version}")
    return os.path.join(base, "kcaa")


@dataclass
class PluginSettings:
    """All user-configurable settings for the KiCad AI Assistant plugin."""

    # LLM provider
    llm_provider: str = "openai"  # "openai" | "anthropic" | "ollama"
    llm_api_key: str = field(default="", repr=False)  # never leak key in logs/repr
    llm_model: str = "gpt-4o"  # model name
    llm_supports_vision: bool = False  # whether the model accepts image input
    llm_base_url: str = (
        ""  # custom API endpoint (overrides provider default, e.g. http://localhost:11434)
    )

    # MCP server
    server_port: int = 0  # 0 = auto-select a free port at startup
    server_log_dir: str = ""  # "" = KiCad user config dir
    python_executable: str = ""  # "" = auto-detect (shutil.which("python3"))

    # UI preferences
    show_tool_log: bool = True  # show the tool-call log by default

    # Context window management
    llm_context_tokens: int = 128_000  # total context window size in tokens
    llm_compact_threshold: float = (
        0.70  # trigger compaction when estimated usage exceeds this fraction
    )
    llm_compact_target_threshold: float = (
        0.49  # post-compaction target fraction (must be < llm_compact_threshold)
    )
    llm_keep_recent_turns: int = 4  # number of latest complete assistant turns to preserve verbatim
    llm_max_tokens: int = 0  # 0 = provider default (only Anthropic uses a hard fallback of 32768)

    # Internal — not shown in settings UI
    config_dir: str = field(default_factory=_get_kcaa_data_dir, repr=False)

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
    def load(cls, config_dir: str | None = None) -> PluginSettings:
        """Load settings from disk, returning defaults if the file doesn't exist."""
        inst = cls()
        if config_dir:
            inst.config_dir = config_dir

        if not os.path.exists(inst.settings_path):
            log.debug("No settings file found; using defaults")
            return inst

        try:
            with open(inst.settings_path, encoding="utf-8") as f:
                data = json.load(f)
            for key, value in data.items():
                if hasattr(inst, key) and key != "config_dir":
                    setattr(inst, key, value)
            log.debug(f"Settings loaded from {inst.settings_path}")
        except (OSError, json.JSONDecodeError) as e:
            log.warning(f"Could not load settings ({e}); using defaults")

        return inst

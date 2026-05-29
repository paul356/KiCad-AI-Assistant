"""
Configuration settings for the KiCad MCP server.

This module provides platform-specific configuration for KiCad integration,
including file paths, extensions, component libraries, and operational constants.
All settings are determined at import time based on the operating system.

All configuration is accessed through the LibraryPathConfig singleton instance.
On import, the module loads environment variables from a ``.env`` file (if present)
so that all downstream configuration reflects user overrides.

Platform Support:
    - macOS (Darwin): Full support with application bundle paths
    - Windows: Standard installation paths
    - Linux: System package paths
    - Unknown: Defaults to macOS paths for compatibility

Dependencies:
    - os: File system operations and environment variables
    - platform: Operating system detection
"""

import logging
import os
import platform

log = logging.getLogger(__name__)


# ---------------------------------------------------------------------------
# Platform detection
# ---------------------------------------------------------------------------

# Determine operating system for platform-specific configuration
# Returns 'Darwin' (macOS), 'Windows', 'Linux', or other
_SYSTEM = platform.system()


class LibraryPathConfig:
    """
    KiCad configuration management.

    This class encapsulates all KiCad-related configuration including:
    - Platform-specific paths (app path, user dir, config dir)
    - Library paths (symbols, footprints, templates)
    - File extensions and constants
    - Environment variable loading from .env files

    All configuration is accessed through the module-level singleton instance.
    The class loads .env at initialization time to ensure environment variables
    are available before computing paths.
    """

    # KiCad version fallback
    _DEFAULT_KICAD_VERSION = "9.0"

    # File extension mappings
    _KICAD_EXTENSIONS = {
        "project": ".kicad_pro",
        "pcb": ".kicad_pcb",
        "schematic": ".kicad_sch",
        "design_rules": ".kicad_dru",
        "worksheet": ".kicad_wks",
        "footprint": ".kicad_mod",
        "netlist": "_netlist.net",
        "kibot_config": ".kibot.yaml",
    }

    _DATA_EXTENSIONS = [
        ".csv",  # BOM or other data
        ".pos",  # Component position file
        ".net",  # Netlist files
        ".zip",  # Gerber files and other archives
        ".drl",  # Drill files
    ]

    # Default parameters for circuit creation
    _CIRCUIT_DEFAULTS = {
        "grid_spacing": 1.0,
        "component_spacing": 10.16,
        "wire_width": 6,
        "text_size": [1.27, 1.27],
        "pin_length": 2.54,
    }

    # Predefined component library mappings
    _COMMON_LIBRARIES = {
        "basic": {
            "resistor": {"library": "Device", "symbol": "R"},
            "capacitor": {"library": "Device", "symbol": "C"},
            "inductor": {"library": "Device", "symbol": "L"},
            "led": {"library": "Device", "symbol": "LED"},
            "diode": {"library": "Device", "symbol": "D"},
        },
        "power": {
            "vcc": {"library": "power", "symbol": "VCC"},
            "gnd": {"library": "power", "symbol": "GND"},
            "+5v": {"library": "power", "symbol": "+5V"},
            "+3v3": {"library": "power", "symbol": "+3V3"},
            "+12v": {"library": "power", "symbol": "+12V"},
            "-12v": {"library": "power", "symbol": "-12V"},
        },
        "connectors": {
            "conn_2pin": {"library": "Connector", "symbol": "Conn_01x02_Male"},
            "conn_4pin": {"library": "Connector_Generic", "symbol": "Conn_01x04"},
            "conn_8pin": {"library": "Connector_Generic", "symbol": "Conn_01x08"},
        },
    }

    # Suggested footprints for common components
    _DEFAULT_FOOTPRINTS = {
        "R": [
            "Resistor_SMD:R_0805_2012Metric",
            "Resistor_SMD:R_0603_1608Metric",
            "Resistor_THT:R_Axial_DIN0207_L6.3mm_D2.5mm_P10.16mm_Horizontal",
        ],
        "C": [
            "Capacitor_SMD:C_0805_2012Metric",
            "Capacitor_SMD:C_0603_1608Metric",
            "Capacitor_THT:C_Disc_D5.0mm_W2.5mm_P5.00mm",
        ],
        "LED": ["LED_SMD:LED_0805_2012Metric", "LED_THT:LED_D5.0mm"],
        "D": ["Diode_SMD:D_SOD-123", "Diode_THT:D_DO-35_SOD27_P7.62mm_Horizontal"],
    }

    # Operation timeout values in seconds
    _TIMEOUT_CONSTANTS = {
        "kicad_cli_version_check": 10.0,
        "kicad_cli_export": 30.0,
        "application_open": 10.0,
        "subprocess_default": 30.0,
    }

    # Progress percentage milestones
    _PROGRESS_CONSTANTS = {
        "start": 10,
        "detection": 20,
        "setup": 30,
        "processing": 50,
        "finishing": 70,
        "validation": 90,
        "complete": 100,
    }

    # UI display configuration
    _DISPLAY_CONSTANTS = {
        "bom_preview_limit": 20,
    }

    # Default project locations
    _DEFAULT_PROJECT_LOCATIONS = [
        "~/Documents/PCB",
        "~/PCB",
        "~/Electronics",
        "~/Projects/Electronics",
        "~/Projects/PCB",
        "~/Projects/KiCad",
    ]

    def __init__(self):
        # Load .env first to get environment variables
        self._load_dotenv()

        # Platform detection
        self._system = _SYSTEM

        # KiCad version
        self._kicad_version = os.environ.get("KICAD_VERSION") or self._DEFAULT_KICAD_VERSION
        self._ver_tag = self._kicad_version.split(".")[0]

        # Platform-specific paths
        self._kicad_user_dir = self._get_default_user_dir()
        self._kicad_app_path = self._resolve_app_path()
        self._kicad_python_base = self._get_python_base()

        # Search paths
        self._additional_search_paths = self._build_search_paths()

        # Library paths (normalised for cross-platform consistency)
        self._kicad_config_dir = os.path.normpath(self._resolve_config_dir())
        self._kicad_symbol_dir = os.path.normpath(self._resolve_symbol_dir())
        self._kicad_footprint_dir = os.path.normpath(self._resolve_footprint_dir())
        self._kicad_3rd_party = os.path.normpath(self._resolve_3rd_party())
        self._kicad_template_dir = os.path.normpath(self._resolve_template_dir())

        # Environment variables for subprocess injection
        self._env_vars = {
            f"KICAD{self._ver_tag}_SYMBOL_DIR": self._kicad_symbol_dir,
            f"KICAD{self._ver_tag}_FOOTPRINT_DIR": self._kicad_footprint_dir,
            f"KICAD{self._ver_tag}_3RD_PARTY": self._kicad_3rd_party,
            f"KICAD{self._ver_tag}_TEMPLATE_DIR": self._kicad_template_dir,
        }

    # ---------------------------------------------------------------------------
    # .env file loading (private)
    # ---------------------------------------------------------------------------

    def _load_dotenv(self, env_file: str = ".env") -> None:
        """Load environment variables from .env file.

        Args:
            env_file: Name of the .env file to find
        """
        env_path = self._find_env_file(env_file)

        if not env_path:
            log.debug(f"No .env file found matching: {env_file}")
            return

        log.info(f"Loading .env file from: {env_path}")

        try:
            with open(env_path) as f:
                for line in f:
                    line = line.strip()

                    # Skip empty lines and comments
                    if not line or line.startswith("#"):
                        continue

                    # Parse key-value pairs
                    if "=" in line:
                        key, value = line.split("=", 1)
                        key = key.strip()
                        value = value.strip()

                        # Remove quotes if present
                        if (
                            value.startswith('"') and value.endswith('"')
                            or value.startswith("'") and value.endswith("'")
                        ):
                            value = value[1:-1]

                        # Expand ~ to user's home directory
                        if "~" in value:
                            value = os.path.expanduser(value)

                        # Set environment variable
                        os.environ[key] = value
                        log.debug(f"Set {key} from .env")

        except Exception:
            log.exception(f"Error loading .env file '{env_path}'")

    def _find_env_file(self, filename: str) -> str | None:
        """Find a .env file in the current directory or parent directories.

        Args:
            filename: Name of the env file to find

        Returns:
            Path to the .env file if found, None otherwise
        """
        current_dir = os.getcwd()
        max_levels = 3

        for _ in range(max_levels):
            env_path = os.path.join(current_dir, filename)
            if os.path.exists(env_path):
                return env_path

            parent_dir = os.path.dirname(current_dir)
            if parent_dir == current_dir:
                break
            current_dir = parent_dir

        return None

    # ---------------------------------------------------------------------------
    # Path resolution helpers (private)
    # ---------------------------------------------------------------------------

    def _get_default_user_dir(self) -> str:
        """Return the platform-specific KiCad user documents directory."""
        if self._system == "Darwin":
            return os.path.expanduser("~/Documents/KiCad")
        elif self._system == "Windows":
            return os.path.expanduser("~/Documents/KiCad")
        elif self._system == "Linux":
            return os.path.expanduser("~/KiCad")
        else:
            return os.path.expanduser("~/Documents/KiCad")

    def _resolve_app_path(self) -> str:
        """Resolve KiCad application path from environment or defaults."""
        env_path = os.environ.get("KICAD_APP_PATH")
        if env_path:
            path = os.path.expanduser(env_path)
        else:
            # Platform defaults
            if self._system == "Darwin":
                path = "/Applications/KiCad/KiCad.app"
            elif self._system == "Windows":
                path = r"C:\Program Files\KiCad"
            elif self._system == "Linux":
                path = "/usr/share/kicad"
            else:
                path = "/Applications/KiCad/KiCad.app"

        if not os.path.isdir(path):
            log.warning(
                "KiCad application path does not exist: %s. "
                "System symbol/footprint/template libraries will be unresolvable. "
                "Set KICAD_APP_PATH in your .env file.",
                path,
            )

        return path

    def _get_python_base(self) -> str:
        """Return the platform-specific KiCad Python framework base path."""
        if self._system == "Darwin":
            return os.path.join(
                self._kicad_app_path, "Contents/Frameworks/Python.framework/Versions"
            )
        else:
            return ""

    def _build_search_paths(self) -> list[str]:
        """Build the list of additional search paths from environment and defaults."""
        paths = []

        # From environment variable
        env_search_paths = os.environ.get("KICAD_SEARCH_PATHS", "")
        if env_search_paths:
            for path in env_search_paths.split(","):
                expanded_path = os.path.expanduser(path.strip())
                if os.path.exists(expanded_path):
                    paths.append(expanded_path)

        # From default locations
        for location in self._DEFAULT_PROJECT_LOCATIONS:
            expanded_path = os.path.expanduser(location)
            if os.path.exists(expanded_path) and expanded_path not in paths:
                paths.append(expanded_path)

        return paths

    @staticmethod
    def _default_symbol_dir(kicad_app_path: str, kicad_version: str) -> str:
        """Return the platform-specific default KiCad system symbols directory."""
        if _SYSTEM == "Darwin":
            return os.path.join(kicad_app_path, "Contents", "SharedSupport", "symbols")
        elif _SYSTEM == "Windows":
            return os.path.join(kicad_app_path, kicad_version, "share", "kicad", "symbols")
        elif _SYSTEM == "Linux":
            return os.path.join(kicad_app_path, "symbols")
        else:
            return os.path.join(kicad_app_path, "symbols")

    @staticmethod
    def _default_footprint_dir(kicad_app_path: str, kicad_version: str) -> str:
        """Return the platform-specific default KiCad system footprints directory."""
        if _SYSTEM == "Darwin":
            return os.path.join(kicad_app_path, "Contents", "SharedSupport", "footprints")
        elif _SYSTEM == "Windows":
            return os.path.join(kicad_app_path, kicad_version, "share", "kicad", "footprints")
        elif _SYSTEM == "Linux":
            return os.path.join(kicad_app_path, "footprints")
        else:
            return os.path.join(kicad_app_path, "footprints")

    @staticmethod
    def _default_config_dir(kicad_version: str) -> str:
        """Return the platform-specific default KiCad configuration directory."""
        if _SYSTEM == "Darwin":
            return os.path.expanduser(f"~/Library/Preferences/kicad/{kicad_version}")
        elif _SYSTEM == "Windows":
            appdata = os.environ.get("APPDATA", os.path.expanduser("~"))
            return os.path.join(appdata, "kicad", kicad_version)
        else:
            return os.path.expanduser(f"~/.config/kicad/{kicad_version}")

    @staticmethod
    def _default_3rd_party(kicad_version: str) -> str:
        """Return the platform-specific default KiCad 3rd-party packages directory."""
        if _SYSTEM == "Darwin":
            return os.path.expanduser(
                f"~/Library/Application Support/kicad/{kicad_version}/3rdparty"
            )
        elif _SYSTEM == "Windows":
            appdata = os.environ.get("APPDATA", os.path.expanduser("~"))
            return os.path.join(appdata, "kicad", kicad_version, "3rdparty")
        else:
            return os.path.expanduser(f"~/.local/share/kicad/{kicad_version}/3rdparty")

    @staticmethod
    def _default_template_dir(kicad_app_path: str, kicad_version: str) -> str:
        """Return the platform-specific default KiCad templates directory."""
        if _SYSTEM == "Darwin":
            return os.path.join(kicad_app_path, "Contents", "SharedSupport", "template")
        elif _SYSTEM == "Windows":
            return os.path.join(kicad_app_path, kicad_version, "share", "kicad", "template")
        else:
            return os.path.join(kicad_app_path, "template")

    def _resolve_config_dir(self) -> str:
        """Resolve KiCad config directory from environment or defaults."""
        env_path = os.environ.get("KICAD_CONFIG_DIR")
        if env_path:
            return os.path.expanduser(os.path.expandvars(env_path))
        return self._default_config_dir(self._kicad_version)

    def _resolve_symbol_dir(self) -> str:
        """Resolve KiCad symbol directory from environment or defaults."""
        env_path = os.environ.get("KICAD_SYMBOL_DIR")
        if env_path:
            return os.path.expanduser(env_path)
        return self._default_symbol_dir(self._kicad_app_path, self._kicad_version)

    def _resolve_footprint_dir(self) -> str:
        """Resolve KiCad footprint directory from environment or defaults."""
        env_path = os.environ.get("KICAD_FOOTPRINT_DIR")
        if env_path:
            return os.path.expanduser(env_path)
        return self._default_footprint_dir(self._kicad_app_path, self._kicad_version)

    def _resolve_3rd_party(self) -> str:
        """Resolve KiCad 3rd-party packages directory from environment or defaults."""
        env_path = os.environ.get("KICAD_3RD_PARTY")
        if env_path:
            return os.path.expanduser(env_path)
        return self._default_3rd_party(self._kicad_version)

    def _resolve_template_dir(self) -> str:
        """Resolve KiCad template directory from environment or defaults."""
        env_path = os.environ.get("KICAD_TEMPLATE_DIR")
        if env_path:
            return os.path.expanduser(env_path)
        return self._default_template_dir(self._kicad_app_path, self._kicad_version)

    # ---------------------------------------------------------------------------
    # Public properties
    # ---------------------------------------------------------------------------

    @property
    def system(self) -> str:
        """Operating system name (Darwin, Windows, Linux, etc.)."""
        return self._system

    @property
    def kicad_version(self) -> str:
        """KiCad version string."""
        return self._kicad_version

    @property
    def kicad_user_dir(self) -> str:
        """Path to the KiCad user documents directory."""
        return self._kicad_user_dir

    @property
    def kicad_app_path(self) -> str:
        """Path to the KiCad application installation."""
        return self._kicad_app_path

    @property
    def kicad_python_base(self) -> str:
        """Path to KiCad's Python framework base (macOS only)."""
        return self._kicad_python_base

    @property
    def additional_search_paths(self) -> list[str]:
        """List of additional project search paths."""
        return self._additional_search_paths.copy()

    @property
    def kicad_config_dir(self) -> str:
        """Path to the KiCad configuration directory (version-aware)."""
        return self._kicad_config_dir

    @property
    def kicad_symbol_dir(self) -> str:
        """Path to the KiCad symbol library directory."""
        return self._kicad_symbol_dir

    @property
    def kicad_footprint_dir(self) -> str:
        """Path to the KiCad footprint library directory."""
        return self._kicad_footprint_dir

    @property
    def kicad_3rd_party(self) -> str:
        """Path to the KiCad 3rd-party packages directory."""
        return self._kicad_3rd_party

    @property
    def kicad_template_dir(self) -> str:
        """Path to the KiCad template directory."""
        return self._kicad_template_dir

    @property
    def symbol_table_file(self) -> str:
        """Path to the sym-lib-table file."""
        return os.path.join(self._kicad_config_dir, "sym-lib-table")

    @property
    def kicad_extensions(self) -> dict[str, str]:
        """KiCad file extension mappings."""
        return self._KICAD_EXTENSIONS.copy()

    @property
    def data_extensions(self) -> list[str]:
        """Data file extensions."""
        return self._DATA_EXTENSIONS.copy()

    @property
    def circuit_defaults(self) -> dict:
        """Default circuit parameters."""
        return self._CIRCUIT_DEFAULTS.copy()

    @property
    def common_libraries(self) -> dict:
        """Predefined component library mappings."""
        return self._COMMON_LIBRARIES.copy()

    @property
    def default_footprints(self) -> dict:
        """Suggested footprints for common components."""
        return self._DEFAULT_FOOTPRINTS.copy()

    @property
    def timeout_constants(self) -> dict[str, float]:
        """Operation timeout values in seconds."""
        return self._TIMEOUT_CONSTANTS.copy()

    @property
    def progress_constants(self) -> dict[str, int]:
        """Progress percentage milestones."""
        return self._PROGRESS_CONSTANTS.copy()

    @property
    def display_constants(self) -> dict[str, int]:
        """UI display configuration values."""
        return self._DISPLAY_CONSTANTS.copy()

    # ---------------------------------------------------------------------------
    # Public methods
    # ---------------------------------------------------------------------------

    def get_env_vars(self) -> dict[str, str]:
        """Get the complete environment variables dictionary for subprocesses."""
        return self._env_vars.copy()

    def get_kcaa_data_dir(self) -> str:
        """Get the kcaa data directory for storing SQLite databases and other persistent data.

        This directory is located under the KiCad config directory (version-aware):
        - Linux: ~/.config/kicad/<version>/kcaa
        - macOS: ~/Library/Preferences/kicad/<version>/kcaa
        - Windows: %APPDATA%/kicad/<version>/kcaa

        Returns:
            Path to the kcaa data directory
        """
        data_dir = os.path.join(self._kicad_config_dir, "kcaa")
        os.makedirs(data_dir, exist_ok=True)
        return data_dir

    def get_env_list(self, env_var: str, default: str = "") -> list[str]:
        """Get a list from a comma-separated environment variable.

        Args:
            env_var: Name of the environment variable
            default: Default value if environment variable is not set

        Returns:
            List of values
        """
        value = os.environ.get(env_var, default)
        if not value:
            return []

        items = [item.strip() for item in value.split(",")]
        return [item for item in items if item]


# ---------------------------------------------------------------------------
# Module-level singleton instance
# ---------------------------------------------------------------------------

# Create the singleton instance at module import time
# This ensures .env is loaded and all configuration is available
config = LibraryPathConfig()

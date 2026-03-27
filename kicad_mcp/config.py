"""
Configuration settings for the KiCad MCP server.

This module provides platform-specific configuration for KiCad integration,
including file paths, extensions, component libraries, and operational constants.
All settings are determined at import time based on the operating system.

Module Variables:
    system (str): Operating system name from platform.system()
    KICAD_VERSION (str): KiCad version string used to construct versioned paths
    KICAD_USER_DIR (str): User's KiCad documents directory
    KICAD_APP_PATH (str): KiCad application installation path
    ADDITIONAL_SEARCH_PATHS (List[str]): Additional project search locations
    DEFAULT_PROJECT_LOCATIONS (List[str]): Common project directory patterns
    KICAD_PYTHON_BASE (str): KiCad Python framework base path (macOS only)
    KICAD_EXTENSIONS (Dict[str, str]): KiCad file extension mappings
    DATA_EXTENSIONS (List[str]): Recognized data file extensions
    CIRCUIT_DEFAULTS (Dict[str, Union[float, List[float]]]): Default circuit parameters
    COMMON_LIBRARIES (Dict[str, Dict[str, Dict[str, str]]]): Component library mappings
    DEFAULT_FOOTPRINTS (Dict[str, List[str]]): Default footprint suggestions per component
    TIMEOUT_CONSTANTS (Dict[str, float]): Operation timeout values in seconds
    PROGRESS_CONSTANTS (Dict[str, int]): Progress reporting percentage values
    DISPLAY_CONSTANTS (Dict[str, int]): UI display configuration values
    LibraryPathConfig: Configuration class for symbol library paths and settings

Platform Support:
    - macOS (Darwin): Full support with application bundle paths
    - Windows: Standard installation paths
    - Linux: System package paths
    - Unknown: Defaults to macOS paths for compatibility

Dependencies:
    - os: File system operations and environment variables
    - platform: Operating system detection
"""

import os
import platform

# Determine operating system for platform-specific configuration
# Returns 'Darwin' (macOS), 'Windows', 'Linux', or other
system = platform.system()

# Platform-specific KiCad installation and user directory paths
# These paths are used for finding KiCad resources and user projects
if system == "Darwin":  # macOS
    KICAD_USER_DIR = os.path.expanduser("~/Documents/KiCad")
    KICAD_APP_PATH = "/Applications/KiCad/KiCad.app"
elif system == "Windows":
    KICAD_USER_DIR = os.path.expanduser("~/Documents/KiCad")
    KICAD_APP_PATH = r"C:\Program Files\KiCad"
elif system == "Linux":
    KICAD_USER_DIR = os.path.expanduser("~/KiCad")
    KICAD_APP_PATH = "/usr/share/kicad"
else:
    # Default to macOS paths if system is unknown for maximum compatibility
    # This ensures the server can start even on unrecognized platforms
    KICAD_USER_DIR = os.path.expanduser("~/Documents/KiCad")
    KICAD_APP_PATH = "/Applications/KiCad/KiCad.app"

# Additional search paths from environment variable KICAD_SEARCH_PATHS
# Users can specify custom project locations as comma-separated paths
ADDITIONAL_SEARCH_PATHS = []
env_search_paths = os.environ.get("KICAD_SEARCH_PATHS", "")
if env_search_paths:
    for path in env_search_paths.split(","):
        expanded_path = os.path.expanduser(path.strip())  # Expand ~ and variables
        if os.path.exists(expanded_path):  # Only add existing directories
            ADDITIONAL_SEARCH_PATHS.append(expanded_path)

# Auto-detect common project locations for convenient project discovery
# These are typical directory names users create for electronics projects
DEFAULT_PROJECT_LOCATIONS = [
    "~/Documents/PCB",  # Common Windows/macOS location
    "~/PCB",  # Simple home directory structure
    "~/Electronics",  # Generic electronics projects
    "~/Projects/Electronics",  # Organized project structure
    "~/Projects/PCB",  # PCB-specific project directory
    "~/Projects/KiCad",  # KiCad-specific project directory
]

# Add existing default locations to search paths
# Avoids duplicates and only includes directories that actually exist
for location in DEFAULT_PROJECT_LOCATIONS:
    expanded_path = os.path.expanduser(location)
    if os.path.exists(expanded_path) and expanded_path not in ADDITIONAL_SEARCH_PATHS:
        ADDITIONAL_SEARCH_PATHS.append(expanded_path)

# Base path to KiCad's Python framework for API access
# macOS bundles Python framework within the application
if system == "Darwin":  # macOS
    KICAD_PYTHON_BASE = os.path.join(
        KICAD_APP_PATH, "Contents/Frameworks/Python.framework/Versions"
    )
else:
    # Linux/Windows use system Python or require dynamic detection
    KICAD_PYTHON_BASE = ""  # Will be determined dynamically in python_path.py


# KiCad file extension mappings for project file identification
# Used by file discovery and validation functions
KICAD_EXTENSIONS = {
    "project": ".kicad_pro",
    "pcb": ".kicad_pcb",
    "schematic": ".kicad_sch",
    "design_rules": ".kicad_dru",
    "worksheet": ".kicad_wks",
    "footprint": ".kicad_mod",
    "netlist": "_netlist.net",
    "kibot_config": ".kibot.yaml",
}

# Additional data file extensions that may be part of KiCad projects
# Includes manufacturing files, component data, and export formats
DATA_EXTENSIONS = [
    ".csv",  # BOM or other data
    ".pos",  # Component position file
    ".net",  # Netlist files
    ".zip",  # Gerber files and other archives
    ".drl",  # Drill files
]

# Default parameters for circuit creation and component placement
# Values in mm unless otherwise specified, following KiCad conventions
CIRCUIT_DEFAULTS = {
    "grid_spacing": 1.0,  # Default grid spacing in mm for user coordinates
    "component_spacing": 10.16,  # Default component spacing in mm
    "wire_width": 6,  # Default wire width in KiCad units (0.006 inch)
    "text_size": [1.27, 1.27],  # Default text size in mm
    "pin_length": 2.54,  # Default pin length in mm
}

# Predefined component library mappings for quick circuit creation
# Maps common component types to their KiCad library and symbol names
# Organized by functional categories: basic, power, connectors
COMMON_LIBRARIES = {
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

# Suggested footprints for common components, ordered by preference
# SMD variants listed first, followed by through-hole alternatives
DEFAULT_FOOTPRINTS = {
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

# Operation timeout values in seconds for external process management
# Prevents hanging operations and provides user feedback
TIMEOUT_CONSTANTS = {
    "kicad_cli_version_check": 10.0,  # Timeout for KiCad CLI version checks
    "kicad_cli_export": 30.0,  # Timeout for KiCad CLI export operations
    "application_open": 10.0,  # Timeout for opening applications (e.g., KiCad)
    "subprocess_default": 30.0,  # Default timeout for subprocess operations
}

# Progress percentage milestones for long-running operations
# Provides consistent progress reporting across different tools
PROGRESS_CONSTANTS = {
    "start": 10,  # Initial progress percentage
    "detection": 20,  # Progress after CLI detection
    "setup": 30,  # Progress after setup complete
    "processing": 50,  # Progress during processing
    "finishing": 70,  # Progress when finishing up
    "validation": 90,  # Progress during validation
    "complete": 100,  # Progress when complete
}

# User interface display configuration values
# Controls how much information is shown in previews and summaries
DISPLAY_CONSTANTS = {
    "bom_preview_limit": 20,  # Maximum number of BOM items to show in preview
}

# KiCad version string — fallback used when KICAD_VERSION env var is not set
KICAD_VERSION = "9.0"

class LibraryPathConfig:
    """
    KiCad symbol library path configuration.

    Reads KICAD_VERSION and each path from the corresponding environment
    variable; if a variable is absent the built-in default is used. The
    resolved values are collected into ``self._env_vars`` so they can be
    injected into subprocess environments that expand ``${VAR}`` URI
    references.
    """

    @staticmethod
    def _default_symbol_dir(kicad_app_path: str) -> str:
        """Return the platform-specific default KiCad system symbols directory."""
        if system == "Darwin":
            return os.path.join(kicad_app_path, "Contents", "SharedSupport", "symbols")
        elif system == "Windows":
            return os.path.join(kicad_app_path, "share", "kicad", "symbols")
        else:
            return os.path.join(kicad_app_path, "symbols")

    @staticmethod
    def _default_config_dir(kicad_version: str) -> str:
        """Return the platform-specific default KiCad configuration directory."""
        if system == "Darwin":
            return os.path.expanduser(f"~/Library/Preferences/kicad/{kicad_version}")
        elif system == "Windows":
            appdata = os.environ.get("APPDATA", os.path.expanduser("~"))
            return os.path.join(appdata, "kicad", kicad_version)
        else:
            return os.path.expanduser(f"~/.config/kicad/{kicad_version}")

    @staticmethod
    def _default_3rd_party(kicad_version: str) -> str:
        """Return the platform-specific default KiCad 3rd-party packages directory."""
        if system == "Darwin":
            return os.path.expanduser(f"~/Library/Application Support/kicad/{kicad_version}/3rdparty")
        elif system == "Windows":
            appdata = os.environ.get("APPDATA", os.path.expanduser("~"))
            return os.path.join(appdata, "kicad", kicad_version, "3rdparty")
        else:
            return os.path.expanduser(f"~/.local/share/kicad/{kicad_version}/3rdparty")

    @staticmethod
    def _default_template_dir(kicad_app_path: str, kicad_version: str) -> str:
        """Return the platform-specific default KiCad templates directory."""
        if system == "Darwin":
            return os.path.join(kicad_app_path, "Contents", "SharedSupport", "template")
        elif system == "Windows":
            return os.path.join(kicad_app_path, "share", "kicad", "template")
        else:
            return os.path.join(kicad_app_path, "template")

    def __init__(self):
        kicad_version = os.environ.get("KICAD_VERSION") or KICAD_VERSION
        _ver_tag = kicad_version.split(".")[0]
        kicad_app_path = os.environ.get("KICAD_APP_PATH") or KICAD_APP_PATH

        self._kicad_config_dir = (
            os.environ.get("KICAD_CONFIG_DIR")
            or self._default_config_dir(kicad_version)
        )
        self._kicad_symbol_dir = (
            os.environ.get("KICAD_SYMBOL_DIR")
            or self._default_symbol_dir(kicad_app_path)
        )
        self._kicad_3rd_party = (
            os.environ.get("KICAD_3RD_PARTY")
            or self._default_3rd_party(kicad_version)
        )
        self._kicad_template_dir = (
            os.environ.get("KICAD_TEMPLATE_DIR")
            or self._default_template_dir(kicad_app_path, kicad_version)
        )

        # Env vars to inject into subprocesses that expand ${VAR} placeholders in library URIs
        self._env_vars = {
            f"KICAD{_ver_tag}_SYMBOL_DIR": self._kicad_symbol_dir,
            f"KICAD{_ver_tag}_3RD_PARTY": self._kicad_3rd_party,
            f"KICAD{_ver_tag}_TEMPLATE_DIR": self._kicad_template_dir
        }

    @property
    def symbol_table_file(self) -> str:
        """Path to the sym-lib-table file."""
        return self._kicad_config_dir + "/sym-lib-table"

    def get_env_vars(self):
        """Get the complete environment variables dictionary."""
        return self._env_vars.copy()

"""
Utility for managing temporary directories.
"""
from typing import List

# List of temporary directories to clean up
_temp_dirs: List[str] = []

def register_temp_dir(temp_dir: str) -> None:
    """Register a temporary directory for cleanup.
    
    Args:
        temp_dir: Path to the temporary directory
    """
    if temp_dir not in _temp_dirs:
        _temp_dirs.append(temp_dir)

def get_temp_dirs() -> List[str]:
    """Get all registered temporary directories.
    
    Returns:
        List of temporary directory paths
    """
    return _temp_dirs.copy()
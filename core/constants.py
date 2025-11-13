"""
Shared constants used across CLI, API, workers, and core modules.
"""

# Image processing defaults
PORTRAIT_SIZE = (900, 1200)
LANDSCAPE_SIZE = (1200, 900)
DEFAULT_MAX_SIZE_KB = 300

# Filenames / paths
FAIL_LOG_PATH = "failures.log"
TEMP_DIR = "temp_download"
OUTPUT_DIR_PREFIX = "optimized_"
ALT_TEXT_MAP = "alt_text_map.json"

# Google API
GOOGLE_DRIVE_SCOPES = ["https://www.googleapis.com/auth/drive"]

# Extensions (without leading dots)
DEFAULT_EXTENSIONS = [
    "jpg",
    "jpeg",
    "png",
    "bmp",
    "tiff",
    "heic",
]

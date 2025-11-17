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

# Google API scopes per integration
GOOGLE_SCOPE_DRIVE = "https://www.googleapis.com/auth/drive"
GOOGLE_SCOPE_DOCS = "https://www.googleapis.com/auth/documents"
# Note: youtube.force-ssl scope is required for captions API access (not just youtube)
# The youtube.force-ssl scope provides full access to manage YouTube account and content, including captions
GOOGLE_SCOPE_YOUTUBE = "https://www.googleapis.com/auth/youtube.force-ssl"
GOOGLE_SCOPE_GMAIL = "https://www.googleapis.com/auth/gmail.readonly"

GOOGLE_INTEGRATION_SCOPES = {
    "drive": [GOOGLE_SCOPE_DRIVE, GOOGLE_SCOPE_DOCS],
    "youtube": [GOOGLE_SCOPE_YOUTUBE],
    "gmail": [GOOGLE_SCOPE_GMAIL],
}

# Legacy/default scopes used by CLI utilities (Drive only)
GOOGLE_OAUTH_SCOPES = [GOOGLE_SCOPE_DRIVE]

# Extensions (without leading dots)
DEFAULT_EXTENSIONS = [
    "jpg",
    "jpeg",
    "png",
    "bmp",
    "tiff",
    "heic",
]

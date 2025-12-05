"""
Logger configuration for the YouTube transcript package.
"""

import logging
import sys
from typing import Optional

def setup_logger(name: str = "youtube_transcript", level: Optional[int] = None) -> logging.Logger:
    """
    Set up and configure the logger.
    
    Args:
        name: Logger name
        level: Logging level (default: INFO)
    
    Returns:
        Configured logger instance
    """
    logger = logging.getLogger(name)
    
    if level is None:
        level = logging.INFO
    
    if not logger.handlers:
        handler = logging.StreamHandler(sys.stdout)
        formatter = logging.Formatter(
            '%(asctime)s - %(name)s - %(levelname)s - %(message)s'
        )
        handler.setFormatter(formatter)
        logger.addHandler(handler)
    
    logger.setLevel(level)
    return logger 
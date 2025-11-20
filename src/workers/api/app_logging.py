"""
Structured logging configuration.
"""

import logging
import sys
import json
from datetime import datetime, timezone
from typing import Any, Dict, Optional
from contextvars import ContextVar

# Request ID context variable
request_id_var: ContextVar[Optional[str]] = ContextVar('request_id', default=None)


class JSONFormatter(logging.Formatter):
    """JSON formatter for structured logging."""
    
    def format(self, record: logging.LogRecord) -> str:
        """Format log record as JSON."""
        # Extract safe fields first for fallback, using the record's original
        # creation time (seconds since epoch) converted to UTC.
        timestamp = datetime.fromtimestamp(record.created, timezone.utc).isoformat()
        level = record.levelname
        logger = record.name
        message = record.getMessage()
        
        log_data: Dict[str, Any] = {
            "timestamp": timestamp,
            "level": level,
            "logger": logger,
            "message": message,
            "module": record.module,
            "function": record.funcName,
            "line": record.lineno,
        }
        
        # Add request ID if available
        request_id = request_id_var.get()
        if request_id:
            log_data["request_id"] = request_id
        
        # Add exception info if present
        if record.exc_info:
            log_data["exception"] = self.formatException(record.exc_info)
        
        # Add extra fields from non-standard LogRecord attributes
        standard_attrs = {
            'name', 'msg', 'args', 'levelname', 'levelno', 'pathname', 'filename',
            'module', 'exc_info', 'exc_text', 'stack_info', 'lineno', 'funcName',
            'created', 'msecs', 'relativeCreated', 'thread', 'threadName',
            'processName', 'process', 'message', 'taskName'
        }
        for key, value in record.__dict__.items():
            if key not in standard_attrs:
                log_data[key] = value
        
        try:
            return json.dumps(log_data, default=str)
        except (TypeError, ValueError) as e:
            # Fallback to safe minimal JSON
            safe_log_data = {
                "timestamp": timestamp,
                "level": level,
                "logger": logger,
                "message": message,
                "serialization_error": {
                    "exception": str(e),
                    "log_data_repr": str(log_data)
                }
            }
            return json.dumps(safe_log_data, default=str)


def setup_logging(level: str = "INFO", use_json: bool = True):
    """Set up logging configuration.
    
    In Cloudflare Workers, logs go to stdout which is captured by the Workers runtime.
    For previews, logs are visible in the Wrangler output and Cloudflare dashboard.
    """
    log_level = getattr(logging, level.upper(), logging.INFO)
    
    # Create handler - stdout works in both local dev and Cloudflare Workers
    handler = logging.StreamHandler(sys.stdout)
    handler.setLevel(log_level)
    
    # Set formatter
    if use_json:
        formatter = JSONFormatter()
    else:
        formatter = logging.Formatter(
            '%(asctime)s - %(name)s - %(levelname)s - %(message)s'
        )
    handler.setFormatter(formatter)
    
    # Configure root logger
    root_logger = logging.getLogger()
    
    # Clear existing handlers to prevent duplicates
    for existing_handler in root_logger.handlers[:]:
        root_logger.removeHandler(existing_handler)
        existing_handler.close()
    root_logger.handlers = []
    
    # Set level and add new handler
    root_logger.setLevel(log_level)
    root_logger.addHandler(handler)
    
    # Set levels for third-party loggers
    logging.getLogger("uvicorn").setLevel(logging.WARNING)
    logging.getLogger("uvicorn.access").setLevel(logging.WARNING)
    
    # Log that logging is configured (helps verify logs are working)
    root_logger.info("Logging configured: level=%s, use_json=%s", level, use_json)
    
    return root_logger


def get_logger(name: str) -> logging.Logger:
    """Get a logger instance."""
    return logging.getLogger(name)


def set_request_id(request_id: str):
    """Set the current request ID."""
    request_id_var.set(request_id)


def get_request_id() -> Optional[str]:
    """Get the current request ID."""
    return request_id_var.get()


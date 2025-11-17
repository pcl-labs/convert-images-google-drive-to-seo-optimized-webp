"""
Custom exception classes for the application.
"""

from typing import Union
from fastapi import HTTPException, status


class APIException(HTTPException):
    """Base API exception."""
    
    def __init__(
        self,
        status_code: int,
        detail: str,
        error_code: str = None,
        headers: dict = None
    ):
        super().__init__(status_code=status_code, detail=detail, headers=headers)
        self.error_code = error_code or f"ERR_{status_code}"


class AuthenticationError(APIException):
    """Authentication failed."""
    
    def __init__(self, detail: str = "Authentication failed"):
        super().__init__(
            status_code=status.HTTP_401_UNAUTHORIZED,
            detail=detail,
            error_code="AUTH_ERROR",
            headers={"WWW-Authenticate": "Bearer"}
        )


class AuthorizationError(APIException):
    """Authorization failed - user doesn't have permission."""
    
    def __init__(self, detail: str = "Insufficient permissions"):
        super().__init__(
            status_code=status.HTTP_403_FORBIDDEN,
            detail=detail,
            error_code="AUTHZ_ERROR"
        )


class NotFoundError(APIException):
    """Resource not found."""
    
    def __init__(self, resource: str = "Resource"):
        super().__init__(
            status_code=status.HTTP_404_NOT_FOUND,
            detail=f"{resource} not found",
            error_code="NOT_FOUND"
        )


class APIValidationError(APIException):
    """Validation error."""
    
    def __init__(self, detail: str = "Validation failed"):
        super().__init__(
            status_code=status.HTTP_422_UNPROCESSABLE_ENTITY,
            detail=detail,
            error_code="VALIDATION_ERROR"
        )


class RateLimitError(APIException):
    """Rate limit exceeded."""
    
    def __init__(self, detail: str = "Rate limit exceeded", retry_after: Union[int, str] = 60):
        retry_after_str = str(retry_after) if isinstance(retry_after, int) else retry_after
        super().__init__(
            status_code=status.HTTP_429_TOO_MANY_REQUESTS,
            detail=detail,
            error_code="RATE_LIMIT_ERROR",
            headers={"Retry-After": retry_after_str}
        )


class JobNotFoundError(NotFoundError):
    """Job not found."""
    
    def __init__(self, job_id: str):
        super().__init__(resource=f"Job {job_id}")


class JobProcessingError(APIException):
    """Job processing error."""
    
    def __init__(self, detail: str = "Job processing failed"):
        super().__init__(
            status_code=status.HTTP_500_INTERNAL_SERVER_ERROR,
            detail=detail,
            error_code="JOB_PROCESSING_ERROR"
        )


class DatabaseError(APIException):
    """Database operation error."""
    
    def __init__(self, detail: str = "Database operation failed"):
        super().__init__(
            status_code=status.HTTP_500_INTERNAL_SERVER_ERROR,
            detail=detail,
            error_code="DATABASE_ERROR"
        )


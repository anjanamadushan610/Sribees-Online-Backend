"""
SRIBEESonline - Gemini API errors

The error every Gemini caller (the shopping assistant, branch briefings, the
admin keyword suggestions) raises and catches. The embedding client that
used to live here served only the semantic POST /search, removed on
2026-09-26 with it: the app searches through GET /products/search.
"""
from typing import Optional


class GeminiAPIError(Exception):
    """Exception raised for Gemini API errors."""

    def __init__(
        self,
        message: str,
        status_code: Optional[int] = None,
        retryable: bool = False
    ):
        super().__init__(message)
        self.status_code = status_code
        self.retryable = retryable

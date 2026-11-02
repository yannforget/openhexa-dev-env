"""Public Python client: explicitly inject credentials; no CLI imports or side effects."""

from .client import APIError
from .resources import API

__all__ = ["API", "APIError"]

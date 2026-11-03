"""Validation for the documented 0.2.0 message envelope."""

from .messages import CONTRACT_VERSION, MESSAGE_TYPES, validate_message

__all__ = ["CONTRACT_VERSION", "MESSAGE_TYPES", "validate_message"]

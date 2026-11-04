"""Deterministic signal targets; no broker or ledger writes."""

from .engine import StaticSignalEngine

__all__ = ["StaticSignalEngine"]

"""Stable error categories exposed at the command-line boundary."""

from __future__ import annotations

from enum import IntEnum


class ExitCode(IntEnum):
    SUCCESS = 0
    USAGE = 2
    COMPATIBILITY = 3
    PERMISSION = 4
    CORRUPT_ARTIFACT = 5
    EXECUTION = 6


class AdvisorCLIError(Exception):
    """A concise, user-facing failure with a stable exit category."""

    def __init__(self, message: str, code: ExitCode = ExitCode.EXECUTION) -> None:
        super().__init__(message)
        self.code = code


__all__ = ["AdvisorCLIError", "ExitCode"]

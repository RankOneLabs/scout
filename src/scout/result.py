"""Lightweight Result type — errors as values."""

from __future__ import annotations

from collections.abc import Callable
from dataclasses import dataclass


@dataclass(frozen=True, slots=True)
class Ok[T]:
    """Successful result carrying a value."""

    value: T


@dataclass(frozen=True, slots=True)
class Err[E]:
    """Failed result carrying an error."""

    error: E


type Result[T, E] = Ok[T] | Err[E]


def map_ok[T, U, E](r: Result[T, E], f: Callable[[T], U]) -> Result[U, E]:
    """Apply ``f`` to the Ok value, propagating Err unchanged."""
    match r:
        case Ok(value):
            return Ok(f(value))
        case Err(_):
            return r


def and_then[T, U, E](r: Result[T, E], f: Callable[[T], Result[U, E]]) -> Result[U, E]:
    """Chain a Result-returning function when ``r`` is Ok."""
    match r:
        case Ok(value):
            return f(value)
        case Err(_):
            return r


def unwrap_or[T, E](r: Result[T, E], default: T) -> T:
    """Return the Ok value or ``default`` when ``r`` is Err."""
    match r:
        case Ok(value):
            return value
        case Err(_):
            return default

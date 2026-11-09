"""Shared primitives: bounded errors, validated limits/settings and exact numeric handling.

Everything here is pure standard library. No module-level mutable state: settings are read per call
so a ``hermes config set`` takes effect without a restart and worker threads share nothing.
"""

from __future__ import annotations

import json
import re
import time
from dataclasses import dataclass, fields
from decimal import (
    ROUND_HALF_EVEN,
    Context,
    Decimal,
    DivisionByZero,
    Inexact,
    InvalidOperation,
    Overflow,
)
from typing import Any, Callable, Optional

MIB = 1024 * 1024

# ── errors ────────────────────────────────────────────────────────────────────────────────────


class LensError(Exception):
    """A refusal or failure with a stable machine code and a short, data-free message."""

    def __init__(self, code: str, message: str, **extra: Any) -> None:
        super().__init__(message)
        self.code = code
        self.message = message
        self.extra = extra

    def as_dict(self) -> dict:
        err = {"code": self.code, "message": self.message[:300]}
        for k, v in self.extra.items():
            err[k] = v
        return {"ok": False, "error": err}


def dumps(obj: Any) -> str:
    """Compact, ASCII-only JSON (control and bidi characters in untrusted data are escaped)."""
    return json.dumps(obj, ensure_ascii=True, separators=(",", ":"), allow_nan=False)


def error_json(exc: LensError) -> str:
    return dumps(exc.as_dict())


def clip(text: str, n: int) -> str:
    """Truncate ``text`` to ``n`` characters with an explicit marker (never silent)."""
    if len(text) <= n:
        return text
    return text[:n] + "...[+%d chars]" % (len(text) - n)


# ── limits ────────────────────────────────────────────────────────────────────────────────────


@dataclass(frozen=True)
class Limits:
    """Every bound enforced while working. ``HARD_CEILINGS`` cannot be raised by configuration."""

    max_bytes: int = 16 * MIB          # whole-file read bound
    max_line_bytes: int = 1 * MIB      # JSONL line / CSV record / JSON array element
    max_rows: int = 200_000            # records scanned
    max_columns: int = 256             # distinct field names
    max_depth: int = 16                # JSON nesting inside one record
    max_groups: int = 1_000            # distinct group keys
    max_group_fields: int = 4
    max_projected: int = 16            # projected columns in rows mode
    max_aggregates: int = 8
    max_filters: int = 16
    max_in_values: int = 50
    max_result_rows: int = 50          # rows returned by rows mode / groups returned
    max_output_chars: int = 20_000     # serialized tool result
    max_cell_chars: int = 200          # strings in results
    time_limit_s: int = 15             # cooperative wall-clock budget per call
    min_bytes: int = 65_536            # read_file interception threshold (not a safety bound)


HARD_CEILINGS = {
    "max_bytes": 16 * MIB,
    "max_line_bytes": 1 * MIB,
    "max_rows": 1_000_000,
    "max_columns": 1024,
    "max_depth": 32,
    "max_groups": 10_000,
    "max_group_fields": 8,
    "max_projected": 64,
    "max_aggregates": 32,
    "max_filters": 64,
    "max_in_values": 500,
    "max_result_rows": 500,
    "max_output_chars": 100_000,
    "max_cell_chars": 2_000,
    "time_limit_s": 60,
    "min_bytes": 16 * MIB,
}


def build_limits(raw: Any) -> tuple[Limits, tuple[str, ...]]:
    """Validate configured limits. Unknown/invalid values fall back to defaults; values above the
    hard ceiling are clamped. Returns ``(limits, warnings)``."""
    defaults = Limits()
    warnings: list[str] = []
    values: dict[str, int] = {}
    if raw is None or raw == {}:
        return defaults, ()
    if not isinstance(raw, dict):
        return defaults, ("limits must be a mapping; defaults used",)
    known = {f.name for f in fields(Limits)}
    for key, val in raw.items():
        if key not in known:
            warnings.append("unknown limit %r ignored" % (str(key)[:40],))
            continue
        if isinstance(val, bool) or not isinstance(val, int) or val < 1:
            warnings.append("limit %s must be a positive integer; default used" % key)
            continue
        if key == "max_output_chars" and val < 1024:
            warnings.append("max_output_chars raised to minimum 1024")
            val = 1024
        ceiling = HARD_CEILINGS[key]
        if val > ceiling:
            warnings.append("limit %s clamped to hard ceiling %d" % (key, ceiling))
            val = ceiling
        values[key] = val
    return Limits(**{**{f.name: getattr(defaults, f.name) for f in fields(Limits)}, **values}), tuple(warnings)


@dataclass(frozen=True)
class Settings:
    roots: tuple[str, ...] = ()
    host_local: bool = False
    intercept_read_file: bool = True
    limits: Limits = Limits()
    warnings: tuple[str, ...] = ()


def load_settings(get_config: Callable[[str, Any], Any]) -> Settings:
    """Read ``plugins.entries.data-lens.settings`` fresh. Any failure yields the fail-closed
    default (no roots => nothing readable)."""
    try:
        warnings: list[str] = []
        raw_roots = get_config("roots", [])
        roots: list[str] = []
        if isinstance(raw_roots, (list, tuple)):
            for r in raw_roots:
                if isinstance(r, str) and r.startswith("/") and "\x00" not in r and r.rstrip("/") != "":
                    roots.append(r)
                else:
                    warnings.append("root entry ignored (must be an absolute path other than '/')")
        elif raw_roots:
            warnings.append("roots must be a list of absolute paths; ignored")
        limits, lw = build_limits(get_config("limits", {}))
        return Settings(
            roots=tuple(roots),
            host_local=get_config("host_local", False) is True,
            intercept_read_file=get_config("intercept_read_file", True) is not False,
            limits=limits,
            warnings=tuple(warnings) + lw,
        )
    except Exception:
        return Settings(warnings=("settings unreadable; failing closed",))


# ── exact numerics ────────────────────────────────────────────────────────────────────────────

MAX_NUMBER_CHARS = 64
MAX_EXPONENT = 100   # Decimal exponent of the last digit must be in [-100, 100]
MAX_DIGITS = 40

# ASCII-only, no whitespace/underscores/inf/nan, at most 4 exponent digits (checked before Decimal()).
_NUM_RE = re.compile(r"[+-]?(?:[0-9]+(?:\.[0-9]*)?|\.[0-9]+)(?:[eE][+-]?[0-9]{1,4})?")

# Exact accumulation: Inexact traps, so a rounded sum is an error rather than a silent approximation.
# Inputs have <=40 digits and exponents in [-100, 100] => any sum fits in ~250 digits (< prec).
EXACT_CTX = Context(prec=400, rounding=ROUND_HALF_EVEN, Emax=999_999, Emin=-999_999,
                    traps=[Inexact, Overflow, InvalidOperation, DivisionByZero])
# Mean: quotient rounded to 28 significant digits, half-even (documented, deterministic).
MEAN_CTX = Context(prec=28, rounding=ROUND_HALF_EVEN, Emax=999_999, Emin=-999_999,
                   traps=[Overflow, InvalidOperation, DivisionByZero])


def decimal_from_token(text: str) -> Optional[Decimal]:
    """Strictly parse a numeric token from its textual form; ``None`` if not a bounded number."""
    if len(text) > MAX_NUMBER_CHARS or not _NUM_RE.fullmatch(text):
        return None
    try:
        d = Decimal(text)
    except (InvalidOperation, ValueError):
        return None
    t = d.as_tuple()
    if not d.is_finite() or not -MAX_EXPONENT <= t.exponent <= MAX_EXPONENT or len(t.digits) > MAX_DIGITS:
        return None
    return d


def dec_str(d: Decimal) -> str:
    """Decimal -> plain positional string (no exponent), scale preserved; ``-0`` is emitted as ``0``."""
    if d.is_zero():
        d = d.copy_abs()
    return format(d, "f")


class NumberOutOfRange(ValueError):
    pass


def json_number_hook(token: str) -> Decimal:
    d = decimal_from_token(token)
    if d is None:
        raise NumberOutOfRange("numeric token out of supported range")
    return d


# ── value classification ──────────────────────────────────────────────────────────────────────

MISSING = object()
NULL, BOOL, NUM, NUMSTR, STR, OBJ, ARR = "null", "bool", "num", "numstr", "str", "obj", "arr"
TYPE_LABEL = {NULL: "null", BOOL: "boolean", NUM: "number", NUMSTR: "numeric_string",
              STR: "string", OBJ: "object", ARR: "array"}


def classify(value: Any, fmt: str) -> tuple:
    """Return ``(kind, decimal, text)``.

    CSV: every cell is text; ``''`` is null; a strictly numeric cell is ``NUM`` (text kept for
    exact grouping). JSON: numbers arrive as Decimal (``NUM``); a *string* that is a strict number is
    ``NUMSTR`` (usable numerically but reported separately); booleans are never numeric.
    """
    if value is MISSING or value is None:
        return (NULL, None, None)
    if isinstance(value, bool):
        return (BOOL, None, None)
    if isinstance(value, Decimal):
        return (NUM, value, None)
    if isinstance(value, str):
        if fmt == "csv":
            if value == "":
                return (NULL, None, None)
            d = decimal_from_token(value)
            return (NUM, d, value) if d is not None else (STR, None, value)
        d = decimal_from_token(value)
        return (NUMSTR, d, value) if d is not None else (STR, None, value)
    if isinstance(value, dict):
        return (OBJ, None, None)
    if isinstance(value, list):
        return (ARR, None, None)
    return (STR, None, str(value))


def check_deadline(deadline):
    if deadline is not None and time.monotonic() > deadline:
        raise LensError("time_limit_exceeded", "the time budget was exhausted; no totals are reported")


def bounded_result(result, max_chars, deadline=None):
    """Check the complete envelope, dropping only explicitly marked profile details."""
    check_deadline(deadline)
    profile = result.get("data_lens", result)
    if "fields" in profile:
        profile["schema_complete"] = not profile.get("fields_omitted", 0)
    encoded = dumps(result)
    while len(encoded) > max_chars and profile.get("fields"):
        profile["fields"].pop()
        profile["fields_omitted"] = profile.get("fields_omitted", 0) + 1
        profile["fields_truncated"] = True
        profile["schema_complete"] = False
        profile["scan_exact"] = True
        profile["exact"] = False
        encoded = dumps(result)
        check_deadline(deadline)
    check_deadline(deadline)
    if len(encoded) > max_chars:
        raise LensError("output_too_large", "complete response exceeds max_output_chars")
    return result

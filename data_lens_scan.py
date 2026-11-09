"""Bounded record iteration over an in-memory snapshot (CSV, JSON top-level array, JSONL).

Supported shapes only; anything else is refused with a stable error code, never guessed:
* CSV: UTF-8 (BOM allowed), comma delimiter, one header row, unique non-empty header names. Rows with
  more cells than the header are refused; shorter rows are accepted and counted (``short_rows``).
* JSON: a top-level array whose elements are all objects. Duplicate keys are refused.
* JSONL/NDJSON: one object per non-blank line.

Every bound (bytes, rows, columns, line/record size, nesting, time) is enforced *during* iteration and
raises ``LensError``; a bound that stops the scan is never turned into an "exact" result.
"""

from __future__ import annotations

import csv
import io
import json
import re
import time
from typing import Iterator

from .data_lens_core import check_deadline, LensError, Limits, NumberOutOfRange, json_number_hook

MAX_FIELD_NAME_CHARS = 128
_WS = re.compile(r"[ \t\n\r]*")
_CHECK_EVERY = 256


def _bad_constant(_name: str):
    raise ValueError("non-standard JSON constant")


def _dict_no_dupes(pairs):
    d = {}
    for k, v in pairs:
        if k in d:
            raise ValueError("duplicate key")
        d[k] = v
    return d


def _decoder() -> json.JSONDecoder:
    return json.JSONDecoder(parse_float=json_number_hook, parse_int=json_number_hook,
                            parse_constant=_bad_constant, object_pairs_hook=_dict_no_dupes)


def _depth_ok(rec: dict, max_depth: int) -> bool:
    """Iterative nesting check for one record (the record itself is depth 1)."""
    stack = [(rec, 1)]
    while stack:
        obj, d = stack.pop()
        if d > max_depth:
            return False
        children = obj.values() if isinstance(obj, dict) else obj
        for c in children:
            if isinstance(c, (dict, list)):
                stack.append((c, d + 1))
    return True


class Records:
    """Iterate ``(index, record_dict)`` (index is 1-based) while tracking structural statistics.

    After exhaustion: ``count`` (records), ``fields`` (ordered field names), ``blank_lines``,
    ``short_rows``. Raises ``LensError`` on any violated bound or unsupported shape.
    """

    def __init__(self, data: bytes, fmt: str, limits: Limits, deadline: float) -> None:
        self.data, self.fmt, self.limits, self.deadline = data, fmt, limits, deadline
        self.count = 0
        self.blank_lines = 0
        self.short_rows = 0
        self._fields: dict[str, None] = {}

    @property
    def fields(self) -> list[str]:
        return list(self._fields)

    def _tick(self, n: int) -> None:
        if time.monotonic() > self.deadline:
            raise LensError("time_limit_exceeded", "the time budget was exhausted before the scan finished; "
                                                   "no totals are reported")

    def _note_fields(self, rec) -> None:
        f = self._fields
        for k in rec:
            if k not in f:
                if len(k) > MAX_FIELD_NAME_CHARS:
                    raise LensError("field_name_too_long", "a field name exceeds %d characters" % MAX_FIELD_NAME_CHARS)
                if len(f) >= self.limits.max_columns:
                    raise LensError("too_many_columns", "more than %d distinct fields" % self.limits.max_columns)
                f[k] = None

    def _count(self) -> None:
        self.count += 1
        if self.count > self.limits.max_rows:
            raise LensError("row_limit_exceeded", "more than %d records; no totals are reported" % self.limits.max_rows)
        self._tick(self.count)

    def __iter__(self) -> Iterator[tuple[int, dict]]:
        check_deadline(self.deadline)
        if self.fmt == "csv":
            return self._csv()
        if self.fmt == "json":
            return self._json()
        if self.fmt == "jsonl":
            return self._jsonl()
        raise LensError("unsupported_file_type", "unknown format")

    # ── CSV ──
    def _csv(self):
        try:
            text = self.data.decode("utf-8-sig")
        except UnicodeDecodeError:
            raise LensError("invalid_encoding", "file is not valid UTF-8") from None
        limit = self.limits.max_line_bytes

        class Gate:
            acc = 0

            def __init__(g):
                g.src = io.StringIO(text, newline="")

            def __iter__(g):
                return g

            def __next__(g):
                line = next(g.src)
                check_deadline(self.deadline)
                g.acc += len(line.encode("utf-8"))
                if g.acc > limit:
                    raise LensError("record_too_large", "a CSV record exceeds %d bytes" % limit)
                return line

        gate = Gate()
        reader = csv.reader(gate, strict=True)
        try:
            header = next(reader)
        except StopIteration:
            raise LensError("invalid_csv", "empty CSV file") from None
        except csv.Error as e:
            raise LensError("invalid_csv", "malformed CSV near line %d" % reader.line_num) from None
        if not header:
            raise LensError("invalid_csv", "first CSV row is empty")
        if len(header) > self.limits.max_columns:
            raise LensError("too_many_columns", "more than %d columns" % self.limits.max_columns)
        if any(h == "" for h in header):
            raise LensError("invalid_csv", "header contains an empty column name")
        if len(set(header)) != len(header):
            raise LensError("invalid_csv", "header contains duplicate column names")
        self._note_fields({h: None for h in header})
        ncol = len(header)
        while True:
            gate.acc = 0
            try:
                row = next(reader)
            except StopIteration:
                return
            except csv.Error:
                raise LensError("invalid_csv", "malformed CSV near line %d" % reader.line_num) from None
            if not row:
                self.blank_lines += 1
                self._tick(self.blank_lines)
                continue
            if len(row) > ncol:
                raise LensError("invalid_csv", "row near line %d has more cells than the header" % reader.line_num)
            if len(row) < ncol:
                self.short_rows += 1
            self._count()
            yield self.count, dict(zip(header, row))

    def _element_end(self, text, start):
        """Bound UTF-8 span and structural depth before allocating decoded values.

        Grammar and duplicate-key validation remain the decoder's responsibility.
        """
        stack = []
        quoted = escaped = False
        size = 0
        for i in range(start, len(text)):
            if (i - start) % 256 == 0:
                check_deadline(self.deadline)
            c = text[i]
            if not quoted and not stack and (c in ",]" or c in " \t\r\n"):
                if i == start:
                    raise LensError("invalid_json", "missing array element")
                return i
            size += len(c.encode("utf-8"))
            if size > self.limits.max_line_bytes:
                raise LensError("record_too_large", "a JSON element exceeds the byte limit")
            if quoted:
                if escaped:
                    escaped = False
                elif c == "\\":
                    escaped = True
                elif c == '"':
                    quoted = False
            elif c == '"':
                quoted = True
            elif c in "{[":
                stack.append(c)
                if len(stack) > self.limits.max_depth:
                    raise LensError("nesting_too_deep", "record nesting exceeds depth limit")
            elif c in "}]":
                if not stack or stack.pop() != ("{" if c == "}" else "["):
                    raise LensError("invalid_json", "mismatched JSON delimiters")
                if not stack:
                    return i + 1
        if quoted or stack or start == len(text):
            raise LensError("invalid_json", "incomplete JSON element")
        return len(text)

    # ── JSON array ──
    def _json(self):
        try:
            text = self.data.decode("utf-8-sig")
        except UnicodeDecodeError:
            raise LensError("invalid_encoding", "file is not valid UTF-8") from None
        dec = _decoder()
        i = _WS.match(text, 0).end()
        if i >= len(text) or text[i] != "[":
            raise LensError("unsupported_json_shape",
                            "only a top-level JSON array of objects is supported (no wrapper objects; "
                            "a nested record key is never guessed). Use read_file with offset/limit for raw access.")
        i = _WS.match(text, i + 1).end()
        if i < len(text) and text[i] == "]":
            i = _WS.match(text, i + 1).end()
            if i != len(text):
                raise LensError("invalid_json", "unexpected content after the top-level array")
            return
        while True:
            i = _WS.match(text, i).end()
            start = i
            end = self._element_end(text, start)
            try:
                rec, i = dec.raw_decode(text, i)
            except NumberOutOfRange:
                raise LensError("number_out_of_range",
                                "a numeric token has too many digits or an exponent beyond +/-%d" % 100) from None
            except RecursionError:
                raise LensError("nesting_too_deep", "JSON nesting exceeds the supported depth") from None
            except json.JSONDecodeError as e:
                raise LensError("invalid_json", "invalid JSON at line %d column %d: %s" % (e.lineno, e.colno, e.msg)) from None
            except ValueError as e:
                raise LensError("invalid_json", "invalid JSON element (%s)" % str(e)[:60]) from None
            if not isinstance(rec, dict):
                raise LensError("unsupported_json_shape", "array elements must all be objects")
            if i != end:
                raise LensError("invalid_json", "malformed JSON element")
            if not _depth_ok(rec, self.limits.max_depth):
                raise LensError("nesting_too_deep", "record nesting exceeds depth %d" % self.limits.max_depth)
            self._note_fields(rec)
            self._count()
            yield self.count, rec
            i = _WS.match(text, i).end()
            if i < len(text) and text[i] == ",":
                i += 1
                continue
            if i < len(text) and text[i] == "]":
                i = _WS.match(text, i + 1).end()
                if i != len(text):
                    raise LensError("invalid_json", "unexpected content after the top-level array")
                return
            raise LensError("invalid_json", "expected ',' or ']' after an array element")

    # ── JSONL ──
    def _jsonl(self):
        data, limit = self.data, self.limits.max_line_bytes
        dec = _decoder()
        pos, n, lineno = 0, len(data), 0
        if data.startswith(b"\xef\xbb\xbf"):
            pos = 3
        while pos < n:
            nl = data.find(b"\n", pos)
            end = n if nl < 0 else nl
            raw = data[pos:end]
            pos = end + 1
            lineno += 1
            if len(raw) > limit:
                raise LensError("line_too_long", "line %d exceeds %d bytes" % (lineno, limit))
            if raw.endswith(b"\r"):
                raw = raw[:-1]
            if not raw.strip():
                self.blank_lines += 1
                self._tick(lineno)
                continue
            try:
                text = raw.decode("utf-8")
            except UnicodeDecodeError:
                raise LensError("invalid_encoding", "line %d is not valid UTF-8" % lineno) from None
            try:
                rec = dec.decode(text)
            except NumberOutOfRange:
                raise LensError("number_out_of_range", "line %d has a numeric token out of supported range" % lineno) from None
            except RecursionError:
                raise LensError("nesting_too_deep", "line %d nesting exceeds the supported depth" % lineno) from None
            except ValueError:
                raise LensError("invalid_jsonl", "line %d is not valid JSON (malformed lines are refused, "
                                                 "not skipped)" % lineno, line=lineno) from None
            if not isinstance(rec, dict):
                raise LensError("unsupported_json_shape", "line %d is not a JSON object" % lineno)
            if not _depth_ok(rec, self.limits.max_depth):
                raise LensError("nesting_too_deep", "line %d nesting exceeds depth %d" % (lineno, self.limits.max_depth))
            self._note_fields(rec)
            self._count()
            yield self.count, rec

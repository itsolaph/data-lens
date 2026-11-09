"""Slice 2: all three formats, structure, heterogeneity, malformed input."""
import json

import pytest

from helpers import write_csv, write_json, write_jsonl


def test_csv_multiline_quoted_bom_and_middle_record(env):
    p = env.root / "m.csv"
    p.write_bytes("﻿id,note\n1,\"line1\nline2\"\n2,\"a,b\"\n3,plain\n".encode())
    prof = env.profile(p)
    assert prof["record_count"] == 3 and prof["format"] == "csv"
    assert [f["name"] for f in prof["fields"]] == ["id", "note"]   # BOM stripped from the first header
    rows = env.query(p, select=["note"], where=[{"field": "id", "op": "eq", "value": 1}])
    assert rows["rows"][0]["values"]["note"] == "line1\nline2"
    assert env.query(p, select=["note"], where=[{"field": "id", "op": "eq", "value": 2}])["rows"][0]["values"]["note"] == "a,b"


def test_csv_short_rows_counted_long_rows_refused(env):
    p = env.root / "s.csv"
    p.write_text("a,b,c\n1,2,3\n4,5\n,,\n")
    prof = env.profile(p)
    assert prof["record_count"] == 3 and prof["short_rows"] == 1
    fa = {f["name"]: f for f in prof["fields"]}
    assert fa["c"]["missing"] == 1 and fa["c"]["null"] == 1 and fa["c"]["present"] == 2
    p2 = env.root / "l.csv"
    p2.write_text("a,b\n1,2,3\n")
    assert env.err(env.profile, p2).code == "invalid_csv"


@pytest.mark.parametrize("body", ["a,a\n1,2\n", "a,\n1,2\n", "", "a,b\n1,\"unterminated\n2,3\n"])
def test_csv_invalid_structures_refused(env, body):
    p = env.root / "bad.csv"
    p.write_text(body)
    assert env.err(env.profile, p).code in {"invalid_csv", "record_too_large"}


def test_json_heterogeneous_fields_accounted(env):
    recs = [{"a": 1, "b": "x"}, {"a": None}, {"c": True, "a": "7"}, {"a": 2.5}]
    p = write_json(env.root / "h.json", recs)
    prof = env.profile(p)
    assert prof["record_count"] == 4
    f = {x["name"]: x for x in prof["fields"]}
    assert f["a"]["present"] == 4 and f["a"]["null"] == 1
    assert f["a"]["types"] == {"null": 1, "number": 2, "numeric_string": 1}
    assert f["b"]["present"] == 1 and f["b"]["missing"] == 3
    assert f["c"]["types"] == {"boolean": 1} and "numeric" not in f["c"]
    assert f["a"]["numeric"] == {"count": 3, "min": "1", "max": "7"}


@pytest.mark.parametrize("body,code", [
    ('{"data": [{"a": 1}]}', "unsupported_json_shape"),
    ("[[1], [2]]", "unsupported_json_shape"),
    ("[1, 2]", "unsupported_json_shape"),
    ('[{"a": 1}, {"a": 2}', "invalid_json"),
    ('[{"a": 1, "a": 2}]', "invalid_json"),
    ('[{"a": NaN}]', "invalid_json"),
    ('[{"a": 1}] trailing', "invalid_json"),
    ("", "unsupported_json_shape"),
])
def test_json_unsupported_or_invalid_labelled_not_guessed(env, body, code):
    p = env.root / "u.json"
    p.write_text(body)
    assert env.err(env.profile, p).code == code


def test_json_empty_array_is_valid_zero_records(env):
    p = env.root / "e.json"
    p.write_text(" [ ] ")
    assert env.profile(p)["record_count"] == 0


def test_jsonl_blank_lines_counted_and_middle_record(env):
    recs = [{"id": i, "v": str(i)} for i in range(50)]
    lines = [json.dumps(r) for r in recs]
    lines.insert(10, "")
    lines.insert(30, "   ")
    p = write_jsonl(env.root / "d.jsonl", None, raw_lines=lines)
    prof = env.profile(p)
    assert prof["record_count"] == 50 and prof["blank_lines"] == 2
    got = env.query(p, select=["v"], where=[{"field": "id", "op": "eq", "value": 25}])
    assert got["rows"][0]["values"]["v"] == "25" and got["rows"][0]["record"] == 26


def test_jsonl_malformed_middle_refused_without_echoing_payload(env):
    lines = [json.dumps({"id": i}) for i in range(20)]
    lines[10] = '{"id": 10, "note": "PAYLOAD-SENTINEL-XYZ" oops}'
    p = write_jsonl(env.root / "bad.jsonl", None, raw_lines=lines)
    e = env.err(env.profile, p)
    assert e.code == "invalid_jsonl" and e.extra["line"] == 11
    assert "PAYLOAD-SENTINEL" not in json.dumps(e.as_dict())


def test_jsonl_non_object_and_bad_encoding(env):
    p = env.root / "n.jsonl"
    p.write_text('{"a":1}\n[1,2]\n')
    assert env.err(env.profile, p).code == "unsupported_json_shape"
    p2 = env.root / "enc.jsonl"
    p2.write_bytes(b'{"a":"\xff\xfe"}\n')
    assert env.err(env.profile, p2).code == "invalid_encoding"


@pytest.mark.parametrize("fmt", ["csv", "json", "jsonl"])
def test_same_data_same_answers_across_formats(env, fmt):
    rows = [{"id": str(i), "cur": "USD" if i % 3 else "EUR", "fee": "%d.10" % i} for i in range(30)]
    if fmt == "csv":
        p = write_csv(env.root / "x.csv", ["id", "cur", "fee"], [[r["id"], r["cur"], r["fee"]] for r in rows])
    elif fmt == "json":
        p = write_json(env.root / "x.json", rows)
    else:
        p = write_jsonl(env.root / "x.jsonl", rows)
    out = env.query(p, group_by=["cur"], aggregates=[{"fn": "sum", "field": "fee", "as": "s"}, {"fn": "count"}])
    got = {g["key"][0]: g["aggregates"] for g in out["groups"]}
    from decimal import Decimal
    for cur in ("USD", "EUR"):
        exp = sum(Decimal(r["fee"]) for r in rows if r["cur"] == cur)
        assert got[cur]["s"] == format(exp, "f")
        assert got[cur]["count"] == sum(1 for r in rows if r["cur"] == cur)

"""Slice 4: every bound is enforced during work and never yields an 'exact' partial result."""
import json
import time

import pytest

from helpers import write_csv, write_json, write_jsonl


def S(env, **lim):
    return env.settings(**lim)


def test_limit_config_validation_cannot_exceed_hard_ceilings(L):
    c = L.core
    lim, warn = c.build_limits({"max_bytes": 10**12, "max_rows": -3, "max_depth": True, "bogus": 1, "time_limit_s": 10**6,
                                "max_groups": 7, "max_output_chars": "x"})
    assert lim.max_bytes == 16 * 1024 * 1024 and lim.time_limit_s == 60 and lim.max_groups == 7
    assert lim.max_rows == c.Limits().max_rows and lim.max_depth == c.Limits().max_depth
    assert len(warn) == 6
    s = c.load_settings(lambda k, d=None: {"roots": ["/a", "rel", 5, "/"], "host_local": "yes",
                                           "limits": {"max_rows": 0}}.get(k, d))
    assert s.roots == ("/a",) and s.host_local is False and s.limits.max_rows == c.Limits().max_rows
    broken = c.load_settings(lambda k, d=None: (_ for _ in ()).throw(RuntimeError("x")))
    assert broken.roots == () and broken.host_local is False


def test_row_cap_refuses_instead_of_partial(env):
    p = write_csv(env.root / "r.csv", ["a"], [[str(i)] for i in range(500)])
    e = env.err(env.profile, p, S(env, max_rows=100))
    assert e.code == "row_limit_exceeded"
    for fmt, w in (("json", write_json), ("jsonl", write_jsonl)):
        q = w(env.root / ("r." + fmt), [{"a": i} for i in range(500)])
        assert env.err(env.query, q, S(env, max_rows=100), aggregates=[{"fn": "count"}]).code == "row_limit_exceeded"


def test_byte_cap(env):
    p = write_csv(env.root / "b.csv", ["a"], [["x" * 100] for _ in range(200)])
    assert env.err(env.profile, p, S(env, max_bytes=1000)).code == "file_too_large"


def test_line_and_record_caps(env):
    j = env.root / "l.jsonl"
    j.write_text(json.dumps({"a": 1}) + "\n" + json.dumps({"a": "x" * 5000}) + "\n")
    assert env.err(env.profile, j, S(env, max_line_bytes=1000)).code == "line_too_long"
    c = env.root / "l.csv"
    write_csv(c, ["a", "b"], [["1", "2"], ["y" * 3000 + "\n" + "z" * 3000, "3"]])
    assert env.err(env.profile, c, S(env, max_line_bytes=1000)).code == "record_too_large"
    # unterminated quote swallowing the rest of the file is bounded by the record cap
    d = env.root / "u.csv"
    d.write_text('a\n"' + "q\n" * 5000)
    assert env.err(env.profile, d, S(env, max_line_bytes=1000)).code == "record_too_large"
    k = env.root / "big.json"
    k.write_text(json.dumps([{"a": "x" * 5000}]))
    assert env.err(env.profile, k, S(env, max_line_bytes=1000)).code == "record_too_large"


def test_column_caps(env):
    cols = ["c%d" % i for i in range(30)]
    c = write_csv(env.root / "w.csv", cols, [["1"] * 30])
    assert env.err(env.profile, c, S(env, max_columns=10)).code == "too_many_columns"
    j = write_json(env.root / "w.json", [{"k%d" % i: 1} for i in range(30)])    # union grows across records
    assert env.err(env.profile, j, S(env, max_columns=10)).code == "too_many_columns"
    n = write_json(env.root / "n.json", [{"x" * 200: 1}])
    assert env.err(env.profile, n).code == "field_name_too_long"


def test_nesting_depth_and_pathological_nesting(env):
    ok = env.root / "d.json"
    ok.write_text(json.dumps([{"a": {"b": {"c": 1}}}]))
    assert env.profile(ok)["fields"][0]["types"] == {"object": 1}
    assert env.err(env.profile, ok, S(env, max_depth=2)).code == "nesting_too_deep"   # record=1, a=2, b=3
    deep = env.root / "deep.json"
    deep.write_text('[{"a": ' + "[" * 200000 + "]" * 200000 + "}]")
    assert env.err(env.profile, deep).code in {"nesting_too_deep", "record_too_large"}
    deepl = env.root / "deep.jsonl"
    deepl.write_text('{"a": ' + "[" * 100000 + "]" * 100000 + "}\n")
    assert env.err(env.profile, deepl).code == "nesting_too_deep"


def test_group_cardinality_cap(env):
    p = write_csv(env.root / "g.csv", ["k"], [[str(i)] for i in range(300)])
    e = env.err(env.query, p, S(env, max_groups=50), group_by=["k"])
    assert e.code == "group_limit_exceeded"
    assert env.query(p, S(env, max_groups=300), group_by=["k"], limit=500)["group_count"] == 300


def test_result_row_projection_and_char_caps(env):
    p = write_csv(env.root / "o.csv", ["a", "b"], [[str(i), "v" * 500] for i in range(400)])
    r = env.query(p, S(env, max_result_rows=20), select=["a", "b"], limit=10_000)
    assert r["returned"] == 20 and r["truncated"] is True and r["matched_count"] == 400
    assert all(len(x["values"]["b"]) < 260 and "chars]" in x["values"]["b"] for x in r["rows"])    # cell truncation is explicit
    t = env.query(p, S(env, max_output_chars=3000, max_cell_chars=300), select=["a", "b"], limit=50)
    assert len(json.dumps(t)) <= 3200 and t["truncated"] is True and t["truncated_reason"] == "max_output_chars"
    assert env.err(env.query, p, S(env, max_projected=1), select=["a", "b"]).code == "invalid_query"
    assert env.err(env.query, p, S(env, max_group_fields=1), group_by=["a", "b"]).code == "invalid_query"
    assert env.err(env.query, p, S(env, max_filters=1), select=["a"], where=[{"field": "a", "op": "eq", "value": 1}] * 2).code == "invalid_query"


def test_profile_output_chars_cap_is_labelled(env):
    cols = ["column_%03d" % i for i in range(200)]
    p = write_csv(env.root / "wide.csv", cols, [["1"] * 200])
    prof = env.profile(p, S(env, max_output_chars=6000))
    assert prof["fields_truncated"] is True and prof["fields_omitted"] > 0 and prof["record_count"] == 1
    assert len(env.L.core.dumps(prof)) <= 6000  # actual compact wire serialization


def test_time_limit_is_cooperative_and_never_exact(env):
    L = env.L
    p = write_csv(env.root / "t.csv", ["a"], [[str(i)] for i in range(2000)])
    src = L.source.load_source(str(p), S(env))
    with pytest.raises(L.core.LensError) as ei:
        L.query.run_query(src, {"aggregates": [{"fn": "count"}]}, S(env).limits, started=time.monotonic() - 10_000)
    assert ei.value.code == "time_limit_exceeded"
    with pytest.raises(L.core.LensError) as ei:
        L.profile.profile_source(src, S(env).limits, started=time.monotonic() - 10_000)
    assert ei.value.code == "time_limit_exceeded"


def test_errors_are_short_and_carry_no_file_payload(env):
    p = env.root / "x.json"
    huge = "SECRETISH-" + "A" * 100_000
    p.write_text(json.dumps([{"a": huge}, {"a": 1}]))
    for spec in ({"aggregates": [{"fn": "sum", "field": "a"}]}, {"select": ["zzz"]}):
        e = env.err(env.query, p, **spec)
        assert len(json.dumps(e.as_dict())) < 1500 and "SECRETISH" not in json.dumps(e.as_dict())
    ok = env.query(p, select=["a"])
    assert len(json.dumps(ok)) < 2000    # the 100k string is cell-truncated, with a marker

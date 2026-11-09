"""Slice 3: exact decimals, null/invalid handling, typed filters, grouping, ordering."""
import json
from decimal import Decimal

import pytest

from helpers import write_csv, write_json, write_jsonl


def test_decimal_sum_is_exact_in_every_representation(env):
    assert 0.1 + 0.2 != 0.3   # the trap this plugin avoids
    for name, writer, recs in [
        ("a.csv", None, None), ("b.json", write_json, [{"v": 0.1}, {"v": 0.2}]),
        ("c.json", write_json, [{"v": "0.1"}, {"v": "0.2"}]), ("d.jsonl", write_jsonl, [{"v": 0.1}, {"v": 0.2}]),
    ]:
        p = env.root / name
        if writer is None:
            write_csv(p, ["v"], [["0.1"], ["0.2"]])
        else:
            writer(p, recs)
        r = env.query(p, aggregates=[{"fn": "sum", "field": "v", "as": "s"}, {"fn": "mean", "field": "v", "as": "m"},
                                       {"fn": "min", "field": "v", "as": "lo"}, {"fn": "max", "field": "v", "as": "hi"}])
        a = r["groups"][0]["aggregates"]
        assert (a["s"], a["m"], a["lo"], a["hi"]) == ("0.3", "0.15", "0.1", "0.2"), name


def test_large_exponents_bounded_and_exact(env):
    p = env.root / "big.json"
    p.write_text('[{"v": 1e100}, {"v": 1}, {"v": 1e-100}]')
    r = env.query(p, aggregates=[{"fn": "sum", "field": "v", "as": "s"}])
    s = r["groups"][0]["aggregates"]["s"]
    from decimal import localcontext
    with localcontext() as ctx:
        ctx.prec = 500   # the reference must not round either (default prec 28 would)
        expected = Decimal("1e100") + 1 + Decimal("1e-100")
    assert Decimal(s) == expected   # exact, not float
    assert len(s) < 400 and "e" not in s.lower()
    # out-of-range number tokens are refused before any Decimal arithmetic or str(Decimal)
    for body in ('[{"v": 1e999999999}]', '[{"v": 1e-999999999}]', '[{"v": %s}]' % ("9" * 5000), '[{"v": 1e101}]'):
        q = env.root / "x.json"
        q.write_text(body)
        assert env.err(env.profile, q).code == "number_out_of_range"
    c = env.root / "x.csv"
    write_csv(c, ["v"], [["1e999999999"], ["5"]])
    prof = env.profile(c)   # not a number => string; never evaluated as Decimal
    assert prof["fields"][0]["types"] == {"number": 1, "string": 1}
    assert env.err(env.query, c, aggregates=[{"fn": "sum", "field": "v"}]).code == "invalid_numeric_value"


def test_bool_never_numeric_and_invalid_not_zeroed(env):
    p = write_json(env.root / "b.json", [{"v": True}, {"v": 5}, {"v": "abc"}, {"v": None}, {}])
    e = env.err(env.query, p, aggregates=[{"fn": "sum", "field": "v"}])
    assert e.code == "invalid_numeric_value" and e.extra["record"] == 1
    r = env.query(p, aggregates=[{"fn": "sum", "field": "v", "as": "s"}, {"fn": "count", "field": "v", "as": "n"}], on_invalid="skip")
    acc = {a["as"]: a for a in r["accounting"]["aggregates"]}
    assert r["groups"][0]["aggregates"]["s"] == "5"          # True was NOT counted as 1, "abc" NOT as 0
    assert acc["s"]["invalid_skipped"] == 2 and acc["s"]["null_or_missing"] == 2 and acc["s"]["used"] == 1
    assert r["groups"][0]["aggregates"]["n"] == 3            # count(field) = non-null values (True, 5, "abc")


def test_all_invalid_or_empty_aggregate_is_null_not_zero(env):
    p = write_json(env.root / "n.json", [{"v": None}, {"w": 1}])
    r = env.query(p, aggregates=[{"fn": "sum", "field": "v", "as": "s"}, {"fn": "mean", "field": "v", "as": "m"},
                                   {"fn": "min", "field": "v", "as": "lo"}, {"fn": "count", "as": "c"}])
    a = r["groups"][0]["aggregates"]
    assert a["s"] is None and a["m"] is None and a["lo"] is None and a["c"] == 2


def test_mean_precision_28_digits_half_even(env):
    p = write_json(env.root / "m.json", [{"v": 1}, {"v": 1}, {"v": 2}])
    m = env.query(p, aggregates=[{"fn": "mean", "field": "v", "as": "m"}])["groups"][0]["aggregates"]["m"]
    assert m == "1." + "3" * 27 and len(m.replace(".", "")) == 28
    p2 = write_json(env.root / "m2.json", [{"v": 2}, {"v": 1}, {"v": 1}, {"v": 1}, {"v": 1}, {"v": 1}, {"v": 1}])
    assert Decimal(env.query(p2, aggregates=[{"fn": "mean", "field": "v", "as": "m"}])["groups"][0]["aggregates"]["m"]) \
        == Decimal(8) / Decimal(7)  # default context is prec 28 too


def test_scale_preserved_no_exponent_serialization(env):
    p = write_json(env.root / "sc.json", [{"v": "1.10"}, {"v": "2.20"}, {"v": "1E+2"}, {"v": "-0"}])
    r = env.query(p, aggregates=[{"fn": "sum", "field": "v", "as": "s"}, {"fn": "min", "field": "v", "as": "lo"},
                                   {"fn": "max", "field": "v", "as": "hi"}])["groups"][0]["aggregates"]
    assert r == {"s": "103.30", "lo": "0", "hi": "100"}
    acc = env.query(p, aggregates=[{"fn": "sum", "field": "v", "as": "s"}])["accounting"]["aggregates"][0]
    assert acc["numeric_strings_used"] == 4   # reported, never silent


def test_group_by_currency_never_converts_and_orders_deterministically(env):
    rows = [["1", "USD", "10.00"], ["2", "EUR", "5.00"], ["3", "USD", "2.50"], ["4", "", "1.00"], ["5", "JPY", "5.00"],
            ["6", "EUR", "5.00"]]
    p = write_csv(env.root / "g.csv", ["id", "cur", "amt"], rows)
    r = env.query(p, group_by=["cur"], aggregates=[{"fn": "sum", "field": "amt", "as": "total"}])
    assert [g["key"][0] for g in r["groups"]] == [None, "EUR", "JPY", "USD"]    # null first, then ascending
    assert {g["key"][0]: g["aggregates"]["total"] for g in r["groups"]} == {None: "1.00", "EUR": "10.00", "JPY": "5.00", "USD": "12.50"}
    o = env.query(p, group_by=["cur"], aggregates=[{"fn": "sum", "field": "amt", "as": "total"}],
                  order_by=[{"by": "total", "dir": "desc"}])
    assert [g["key"][0] for g in o["groups"]] == ["USD", "EUR", "JPY", None]    # tie EUR/JPY(10 vs 5): by value; ties by key asc
    t = env.query(p, group_by=["cur"], aggregates=[{"fn": "max", "field": "amt", "as": "mx"}],
                  order_by=[{"by": "mx", "dir": "desc"}])
    # mx: USD 10.00, EUR 5.00, JPY 5.00 tie -> key ascending; None (1.00) lowest
    assert [g["key"][0] for g in t["groups"]] == ["USD", "EUR", "JPY", None]


def test_group_keys_are_typed_and_not_merged(env):
    p = write_json(env.root / "t.json", [{"k": 1}, {"k": "1"}, {"k": True}, {"k": None}, {}, {"k": 1.0}, {"k": 1}])
    r = env.query(p, group_by=["k"])
    got = {(tuple(g["key"]), tuple(g["key_types"])): g["aggregates"]["count"] for g in r["groups"]}
    assert got[((None,), ("null",))] == 2        # null + missing share the null group (documented)
    assert got[((True,), ("boolean",))] == 1
    assert got[(("1",), ("number",))] == 2 and got[(("1.0",), ("number",))] == 1
    assert got[(("1",), ("string",))] == 1
    n = env.root / "nest.json"
    n.write_text('[{"k": {"a": 1}}]')
    assert env.err(env.query, n, group_by=["k"]).code == "unsupported_group_field"


def test_typed_filters_and_accounting(env):
    p = write_json(env.root / "f.json", [
        {"id": 1, "amt": 10, "s": "b", "ok": True}, {"id": 2, "amt": "20", "s": "a", "ok": False},
        {"id": 3, "amt": None, "s": "c"}, {"id": 4, "amt": "abc", "s": 7, "ok": "true"}, {"id": 5, "amt": 30.5}])
    def ids(**f):
        r = env.query(p, select=["id"], where=[f])
        return [x["values"]["id"] for x in r["rows"]], r["accounting"]["filters"][0]
    got, acc = ids(field="amt", op="gte", value="20", **{"as": "number"})
    assert got == ["2", "5"] and acc["null_or_missing"] == 1 and acc["incomparable"] == 1    # "abc" not coerced
    assert ids(field="amt", op="gt", value=10)[0] == ["2", "5"]
    assert ids(field="ok", op="eq", value=True)[0] == ["1"]            # string "true" is not boolean true
    assert ids(field="ok", op="eq", value=None)[0] == ["3", "5"]       # null test includes missing
    assert ids(field="ok", op="ne", value=None)[0] == ["1", "2", "4"]
    assert ids(field="s", op="in", value=["a", "c"])[0] == ["2", "3"]
    assert ids(field="s", op="contains", value="a")[0] == ["2"]
    assert ids(field="s", op="lt", value="b")[0] == ["2"]
    assert ids(field="id", op="in", value=[1, 5], **{"as": "number"})[0] == ["1", "5"]


def test_csv_typed_filters_text_vs_number(env):
    p = write_csv(env.root / "c.csv", ["id", "code", "flag"], [["1", "007", "true"], ["2", "7", "FALSE"], ["3", "", "x"]])
    num = env.query(p, select=["id"], where=[{"field": "code", "op": "eq", "value": 7}])
    assert [r["values"]["id"] for r in num["rows"]] == ["1", "2"]
    txt = env.query(p, select=["id"], where=[{"field": "code", "op": "eq", "value": "007"}])
    assert [r["values"]["id"] for r in txt["rows"]] == ["1"]
    b = env.query(p, select=["id"], where=[{"field": "flag", "op": "eq", "value": False}])
    assert [r["values"]["id"] for r in b["rows"]] == ["2"]
    g = env.query(p, group_by=["code"])
    assert sorted(x["key"][0] for x in g["groups"] if x["key"][0] is not None) == ["007", "7"]   # zero padding not merged


def test_rows_mode_pagination_and_unknown_field(env):
    p = write_csv(env.root / "p.csv", ["i"], [[str(i)] for i in range(25)])
    r = env.query(p, select=["i"], where=[{"field": "i", "op": "gte", "value": 5}], limit=4, offset=3)
    assert [x["values"]["i"] for x in r["rows"]] == ["8", "9", "10", "11"]
    assert r["matched_count"] == 20 and r["truncated"] is True and r["record_count"] == 25
    e = env.err(env.query, p, select=["nope"])
    assert e.code == "unknown_field" and "i" in e.message
    assert env.err(env.query, p, select=["i"], group_by=["i"]).code == "invalid_query"
    assert env.err(env.query, p, aggregates=[{"fn": "sum"}]).code == "invalid_query"
    assert env.err(env.query, p, aggregates=[{"fn": "avg", "field": "i"}]).code == "invalid_query"
    assert env.err(env.query, p, select=["i"], where=[{"field": "i", "op": "eval", "value": 1}]).code == "invalid_query"


def test_zero_matches(env):
    p = write_csv(env.root / "z.csv", ["a"], [["1"]])
    r = env.query(p, where=[{"field": "a", "op": "eq", "value": 99}], aggregates=[{"fn": "count"}, {"fn": "sum", "field": "a", "as": "s"}])
    assert r["matched_count"] == 0 and r["groups"][0]["aggregates"] == {"count": 0, "s": None}
    r2 = env.query(p, where=[{"field": "a", "op": "eq", "value": 99}], group_by=["a"])
    assert r2["groups"] == [] and r2["group_count"] == 0

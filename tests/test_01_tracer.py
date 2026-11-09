"""Slice 1 (tracer): a real default read_file of a large CSV is swapped for a structural map by the
real middleware chain, and a different tool call aggregates records from the middle of the file."""
import csv
import json
from decimal import Decimal


def make_csv(path, n=4000):
    with open(path, "w", newline="", encoding="utf-8") as f:
        w = csv.writer(f)
        w.writerow(["trade_id", "symbol", "qty", "fee", "fee_currency", "note"])
        for i in range(n):
            cur = "USD"
            fee = "0.10"
            if i == n // 2:
                cur, fee = "EUR", "9999.99"   # anomaly in the middle of the file
            w.writerow([i, "SYN-%d" % (i % 7), i % 13 + 1, fee, cur, "synthetic filler text " * 3])
    return path


def test_tracer_swap_and_middle_aggregate(tmp_path, run_driver):
    root = tmp_path / "root"
    root.mkdir()
    f = make_csv(root / "trades.csv")
    res = run_driver(f"""
        boot(HOME, roots=[{str(root)!r}])
        raw_map = call("read_file", {{"path": {str(f)!r}}})
        q = call("data_lens_query", {{"path": {str(f)!r},
            "group_by": ["fee_currency"],
            "aggregates": [{{"fn": "count"}}, {{"fn": "sum", "field": "fee", "as": "fee_sum"}}]}})
        emit({{"map": raw_map, "query": q}})
    """)
    m = json.loads(res["map"])
    assert m["data_lens"]["record_count"] == 4000
    assert m["data_lens"]["format"] == "csv"
    assert "content" not in m
    q = json.loads(res["query"])
    got = {g["key"][0]: g for g in q["groups"]}
    with open(f, newline="", encoding="utf-8") as fh:
        rows = list(csv.DictReader(fh))
    exp_eur = sum(Decimal(r["fee"]) for r in rows if r["fee_currency"] == "EUR")
    exp_usd = sum(Decimal(r["fee"]) for r in rows if r["fee_currency"] == "USD")
    assert got["EUR"]["aggregates"]["fee_sum"] == format(exp_eur, "f")
    assert got["USD"]["aggregates"]["fee_sum"] == format(exp_usd, "f")

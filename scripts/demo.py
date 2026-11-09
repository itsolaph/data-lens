#!/usr/bin/env python3
"""Data Lens demo + benchmark (repeatable; nothing is precomputed).

What it does
  1. Generates clearly labelled SYNTHETIC trading fixtures (CSV, JSON array, JSONL) under a temporary
     demo root, each > 1 MiB, with one anomalous record in the MIDDLE of the file.
  2. Boots the REAL Hermes plugin loader in an isolated HERMES_HOME (the plugin directory is copied
     verbatim into <home>/plugins/data-lens and enabled through config.yaml).
  3. Calls the REAL built-in ``read_file`` (a) directly, as the baseline default page, and (b) through
     the real dispatcher + middleware chain (``model_tools.handle_function_call``), then calls the
     plugin tools (``data_lens_profile`` / ``data_lens_query``).
  4. Recomputes every aggregate with an independent csv/json + Decimal reference (no plugin code) and
     compares exactly.
  5. Reports context size in characters and bytes. A token count is given ONLY if ``tiktoken`` is
     importable; otherwise it is reported as unavailable (nothing is estimated).

Run it with the Python interpreter of your Hermes environment (it needs ``hermes_cli``):

    <hermes-python> scripts/demo.py --out ./demo-out [--rows 10000] [--seed 7]

Writes ``evidence.json`` (machine readable) and ``transcript.txt`` (human readable) to --out. Paths
under the temporary demo root are replaced by ``<DEMO_ROOT>`` / ``<HERMES_HOME>`` in both files.
Exit status is 0 only if every comparison matched.

Honesty notes: the baseline read still happens (Data Lens reduces context payload, not I/O); the
"default page" comparison and the "whole file" comparison are reported separately; timings are wall
clock on this machine and are not a speed claim.
"""

from __future__ import annotations

import argparse
import csv
import hashlib
import json
import os
import random
import shutil
import sys
import tempfile
import time
from decimal import ROUND_HALF_EVEN, Context, Decimal, localcontext
from pathlib import Path

REPO = Path(__file__).resolve().parent.parent
PLUGIN_FILES = ["plugin.yaml", "__init__.py", "LICENSE"] + sorted(p.name for p in REPO.glob("data_lens_*.py"))
CURRENCIES = ["USD"] * 17 + ["EUR", "USDT", "BTC"]
SYMBOLS = ["SYN-AAA", "SYN-BBB", "SYN-CCC", "SYN-DDD"]
FEES = ["0.10", "0.20", "0.30", "0.05", "0.15"]
ANOMALY_FEE = "12345.67"
ANOMALY_CURRENCY = "EUR"
ANOMALY_NOTE = "SYNTHETIC ANOMALY: fee outlier in the middle of the file"


def gen_rows(n: int, seed: int) -> list[dict]:
    rng = random.Random(seed)
    rows = []
    for i in range(n):
        qty = rng.randint(1, 500)
        price = Decimal(rng.randint(100, 99999)) / Decimal(100)
        rows.append({
            "trade_id": i + 1, "symbol": rng.choice(SYMBOLS), "side": rng.choice(["buy", "sell"]),
            "qty": qty, "price": str(price), "notional": str((price * qty).quantize(Decimal("0.01"))),
            "fee": rng.choice(FEES), "fee_currency": rng.choice(CURRENCIES),
            "note": "synthetic fixture row, not real trading data " + "x" * 40,
        })
    mid = n // 2
    rows[mid]["fee"], rows[mid]["fee_currency"], rows[mid]["note"] = ANOMALY_FEE, ANOMALY_CURRENCY, ANOMALY_NOTE
    return rows


def write_fixtures(root: Path, rows: list[dict]) -> dict[str, Path]:
    cols = list(rows[0])
    paths = {"csv": root / "synthetic_trades.csv", "json": root / "synthetic_trades.json",
             "jsonl": root / "synthetic_trades.jsonl"}
    with open(paths["csv"], "w", newline="", encoding="utf-8") as f:
        w = csv.DictWriter(f, fieldnames=cols)
        w.writeheader()
        w.writerows(rows)
    # JSON / JSONL carry `fee` as a JSON number token (e.g. 0.10) and `qty` as an integer token.
    def js(r):  # keep the exact decimal text of the fee as a number token
        return json.dumps(r, separators=(",", ":")).replace('"fee":"%s"' % r["fee"], '"fee":%s' % r["fee"])
    paths["json"].write_text("[" + ",".join(js(r) for r in rows) + "]", encoding="utf-8")
    paths["jsonl"].write_text("\n".join(js(r) for r in rows) + "\n", encoding="utf-8")
    for p in paths.values():
        assert p.stat().st_size > 1024 * 1024, "fixture must exceed 1 MiB (raise --rows)"
    return paths


# ── independent reference (stdlib csv/json + Decimal only; no plugin code) ─────────────────────────


def reference(path: Path, fmt: str) -> dict:
    if fmt == "csv":
        with open(path, newline="", encoding="utf-8") as f:
            recs = list(csv.DictReader(f))
    elif fmt == "json":
        recs = json.loads(path.read_text(encoding="utf-8"), parse_float=Decimal, parse_int=Decimal)
    else:
        recs = [json.loads(l, parse_float=Decimal, parse_int=Decimal) for l in path.read_text(encoding="utf-8").splitlines() if l.strip()]
    by: dict[str, list[Decimal]] = {}
    for r in recs:
        by.setdefault(r["fee_currency"], []).append(Decimal(str(r["fee"])))
    out = {}
    with localcontext(Context(prec=400)) as exact:
        for cur, vals in by.items():
            total = sum(vals, Decimal(0))
            with localcontext(Context(prec=28, rounding=ROUND_HALF_EVEN)):
                mean = total / len(vals)
            out[cur] = {"count": len(vals), "sum": format(total, "f"), "min": format(min(vals), "f"),
                        "max": format(max(vals), "f"), "mean": format(mean, "f")}
    big = [i + 1 for i, r in enumerate(recs) if Decimal(str(r["fee"])) > Decimal("1000")]
    return {"records": len(recs), "by_currency": out, "records_with_fee_gt_1000": big}


# ── helpers ──────────────────────────────────────────────────────────────────────────────────────


def sha256(p: Path) -> str:
    return hashlib.sha256(p.read_bytes()).hexdigest()


def sizes(s: str) -> dict:
    return {"chars": len(s), "bytes_utf8": len(s.encode("utf-8"))}


def tokenizer():
    try:
        import tiktoken
        enc = tiktoken.get_encoding("cl100k_base")
        return "tiktoken:cl100k_base", lambda s: len(enc.encode(s))
    except Exception:
        return None, None


def boot(home: Path, root: Path):
    (home / "plugins" / "data-lens").mkdir(parents=True)
    for name in PLUGIN_FILES:
        shutil.copy2(REPO / name, home / "plugins" / "data-lens" / name)
    (home / "config.yaml").write_text(
        "plugins:\n  enabled: [data-lens]\n  entries:\n    data-lens:\n      settings:\n"
        "        roots: [%s]\n        host_local: true\n" % json.dumps(str(root)))
    os.environ["HOME"] = str(home.parent / "user_home")
    os.environ["HERMES_HOME"] = str(home)
    os.environ.pop("TERMINAL_ENV", None)
    from hermes_cli.plugins import get_plugin_manager
    mgr = get_plugin_manager()
    mgr.discover_and_load(force=True)
    me = mgr._plugins.get("data-lens")
    if me is None or me.error:
        raise SystemExit("plugin failed to load: %r" % (getattr(me, "error", "not discovered"),))
    return {"tools": list(me.tools_registered), "middleware": list(me.middleware_registered)}


def main() -> int:
    ap = argparse.ArgumentParser(description=__doc__.split("\n\n")[0])
    ap.add_argument("--out", default="demo-out", help="directory for evidence.json and transcript.txt")
    ap.add_argument("--rows", type=int, default=10_000)
    ap.add_argument("--seed", type=int, default=7)
    args = ap.parse_args()
    try:
        import hermes_cli  # noqa: F401
    except ImportError:
        print("hermes_cli is not importable: run this script with the Python interpreter of your Hermes environment.")
        return 2
    out = Path(args.out).resolve()
    out.mkdir(parents=True, exist_ok=True)
    tmp = Path(tempfile.mkdtemp(prefix="data-lens-demo-"))
    saved_env = {k: os.environ.get(k) for k in ("HOME", "HERMES_HOME", "TERMINAL_ENV")}
    try:
        root, home = tmp / "root", tmp / "hermes_home"
        root.mkdir()
        home.mkdir()
        tok_name, tok = tokenizer()
        lines: list[str] = []

        def say(s: str = "") -> None:
            lines.append(s)
            print(s)

        rows = gen_rows(args.rows, args.seed)
        paths = write_fixtures(root, rows)
        hashes_before = {k: sha256(p) for k, p in paths.items()}
        say("Data Lens demo  (SYNTHETIC fixtures; seed=%d rows=%d)" % (args.seed, args.rows))
        say("anomalous record: #%d (fee %s %s) in the middle of every file" % (args.rows // 2 + 1, ANOMALY_FEE, ANOMALY_CURRENCY))
        reg = boot(home, root)
        say("plugin loaded by the real PluginManager: tools=%s middleware=%s" % (reg["tools"], reg["middleware"]))
        import model_tools
        from tools.registry import registry

        evidence = {"synthetic": True, "rows": args.rows, "seed": args.seed, "tokenizer": tok_name,
                    "plugin_registrations": reg, "formats": {}, "all_match": True}
        for fmt, path in paths.items():
            say("\n=== %s  (%d bytes) ===" % (fmt.upper(), path.stat().st_size))
            ref = reference(path, fmt)
            t0 = time.perf_counter()
            base = registry.dispatch("read_file", {"path": str(path)}, task_id="baseline-" + fmt)   # built-in, no middleware
            t_base = time.perf_counter() - t0
            t0 = time.perf_counter()
            mapped = model_tools.handle_function_call("read_file", {"path": str(path)}, task_id="chain-" + fmt)
            t_chain = time.perf_counter() - t0
            bd, md = json.loads(base), json.loads(mapped)
            assert md.get("data_lens_map") is True, "middleware did not swap the page: %s" % mapped[:200]
            shown_lines = bd["content"].count("\n") + 1
            say("default read_file page: %d chars, shows ~%d of %s lines; anomaly visible in page: %s" % (
                len(base), shown_lines, bd.get("total_lines"), ANOMALY_NOTE.split(":")[0] in bd["content"]))
            say("Data Lens map: %d chars (record_count=%s, fingerprint=%s...)" % (
                len(mapped), md["data_lens"]["record_count"], md["data_lens"]["fingerprint"][:19]))
            fp = md["data_lens"]["fingerprint"]
            qcalls = {
                "grouped_by_currency": {"path": str(path), "expect_fingerprint": fp, "group_by": ["fee_currency"],
                                        "aggregates": [{"fn": "count"}, {"fn": "sum", "field": "fee", "as": "sum_fee"},
                                                       {"fn": "min", "field": "fee", "as": "min_fee"},
                                                       {"fn": "max", "field": "fee", "as": "max_fee"},
                                                       {"fn": "mean", "field": "fee", "as": "mean_fee"}],
                                        "order_by": [{"by": "sum_fee", "dir": "desc"}]},
                "outliers_fee_gt_1000": {"path": str(path), "expect_fingerprint": fp, "select": ["trade_id", "fee", "fee_currency", "note"],
                                         "where": [{"field": "fee", "op": "gt", "value": "1000", "as": "number"}]},
            }
            results, sizes_q = {}, {}
            for name, a in qcalls.items():
                t0 = time.perf_counter()
                r = model_tools.handle_function_call("data_lens_query", a, task_id="chain-" + fmt)
                results[name] = (json.loads(r), time.perf_counter() - t0)
                sizes_q[name] = sizes(r)
                say("data_lens_query %-22s -> %d chars" % (name, len(r)))
                say("  " + r[:700] + (" ..." if len(r) > 700 else ""))
            prof_raw = model_tools.handle_function_call("data_lens_profile", {"path": str(path)}, task_id="chain-" + fmt)
            # --- compare with the independent reference --------------------------------------------
            got = {g["key"][0]: g["aggregates"] for g in results["grouped_by_currency"][0]["groups"]}
            checks = {}
            for cur, exp in ref["by_currency"].items():
                g = got.get(cur, {})
                checks[cur] = (g.get("count") == exp["count"] and g.get("sum_fee") == exp["sum"] and g.get("min_fee") == exp["min"]
                               and g.get("max_fee") == exp["max"] and g.get("mean_fee") == exp["mean"])
            outl = [r["record"] for r in results["outliers_fee_gt_1000"][0]["rows"]]
            ok = (all(checks.values()) and set(got) == set(ref["by_currency"]) and outl == ref["records_with_fee_gt_1000"]
                  and md["data_lens"]["record_count"] == ref["records"] and json.loads(prof_raw)["fingerprint"] == fp)
            evidence["all_match"] &= bool(ok)
            say("independent Decimal reference matches every aggregate: %s  (USD fee sum: %s)" % (ok, ref["by_currency"]["USD"]["sum"]))
            # --- context accounting ---------------------------------------------------------------
            plugin_ctx = sizes(mapped)["chars"] + sum(v["chars"] for v in sizes_q.values())
            plugin_bytes = sizes(mapped)["bytes_utf8"] + sum(v["bytes_utf8"] for v in sizes_q.values())
            rec = {
                "file_bytes": path.stat().st_size,
                "default_read_page": sizes(base), "default_page_lines_shown": shown_lines, "total_lines": bd.get("total_lines"),
                "anomaly_visible_in_default_page": ANOMALY_NOTE.split(":")[0] in bd["content"],
                "map": sizes(mapped), "queries": sizes_q,
                "map_plus_queries": {"chars": plugin_ctx, "bytes_utf8": plugin_bytes},
                "default_page_vs_map_chars": {"page": len(base), "map": len(mapped),
                                              "map_over_page_ratio": round(len(mapped) / len(base), 4)},
                "default_page_vs_map_plus_queries_chars": {"page": len(base), "map_plus_queries": plugin_ctx,
                                                           "ratio": round(plugin_ctx / len(base), 4)},
                "whole_file_vs_map_plus_queries_bytes": {"file": path.stat().st_size, "map_plus_queries": plugin_bytes,
                                                         "ratio": round(plugin_bytes / path.stat().st_size, 5)},
                "wall_seconds": {"builtin_read_file": round(t_base, 4), "read_file_through_chain_incl_map": round(t_chain, 4),
                                 "queries": {k: round(v[1], 4) for k, v in results.items()}},
                "reference_match": bool(ok), "per_currency_match": checks,
                "reference": ref,
            }
            if tok:
                rec["tokens"] = {"page": tok(base), "map_plus_queries": tok(mapped) + sum(tok(json.dumps(v[0])) for v in results.values())}
            evidence["formats"][fmt] = rec
            say("context: default page %d chars  vs  map %d chars  vs  map+queries %d chars  (whole file %d bytes)" % (
                len(base), len(mapped), plugin_ctx, path.stat().st_size))
            say("tokens: %s" % (rec.get("tokens") if tok else "tokenizer unavailable (no tiktoken); not estimated"))
        hashes_after = {k: sha256(p) for k, p in paths.items()}
        evidence["source_sha256_unchanged"] = hashes_before == hashes_after
        evidence["source_sha256"] = hashes_after
        say("\nsource files unchanged (sha256 before == after): %s" % evidence["source_sha256_unchanged"])
        say("ALL COMPARISONS MATCH: %s" % evidence["all_match"])
        ok_all = evidence["all_match"] and evidence["source_sha256_unchanged"]
        # sanitize temp paths in everything we persist
        def clean(s: str) -> str:
            return s.replace(str(root), "<DEMO_ROOT>").replace(str(home), "<HERMES_HOME>").replace(str(tmp), "<DEMO_TMP>")
        (out / "evidence.json").write_text(clean(json.dumps(evidence, indent=2, default=str)), encoding="utf-8")
        (out / "transcript.txt").write_text(clean("\n".join(lines)) + "\n", encoding="utf-8")
        print("\nwrote %s and %s" % (out / "evidence.json", out / "transcript.txt"))
        return 0 if ok_all else 1
    finally:
        shutil.rmtree(tmp, ignore_errors=True)
        for key, value in saved_env.items():
            if value is None:
                os.environ.pop(key, None)
            else:
                os.environ[key] = value


if __name__ == "__main__":
    sys.exit(main())

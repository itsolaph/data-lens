"""Slice 6: real PluginManager discovery + real dispatcher/middleware chain (subprocess per scenario).

Dispatch path exercised: model_tools.handle_function_call -> run_tool_execution_middleware ->
registry.dispatch -> built-in read_file. The agent loop (agent/tool_executor.py) wraps the same
middleware around its approval/hook stage and calls handle_function_call(skip_tool_execution_middleware=True);
that composition is exercised too, but approvals/pre_tool_call hooks/guardrails are NOT (no full agent).
"""
import csv
import json
import os

import pytest

from test_01_tracer import make_csv


def big_rows(n=3000):
    return [{"id": i, "cur": "EUR" if i == n // 2 else "USD", "fee": "9999.99" if i == n // 2 else "0.10",
             "pad": "synthetic filler text " * 3} for i in range(n)]


def make_all(root, n=3000):
    rows = big_rows(n)
    with open(root / "t.csv", "w", newline="") as f:
        w = csv.DictWriter(f, fieldnames=list(rows[0]))
        w.writeheader()
        w.writerows(rows)
    (root / "t.json").write_text(json.dumps(rows))
    (root / "t.jsonl").write_text("\n".join(json.dumps(r) for r in rows) + "\n")
    return rows


@pytest.mark.parametrize("ext", ["csv", "json", "jsonl"])
def test_all_formats_swap_and_middle_record_query(tmp_path, run_driver, ext):
    root = tmp_path / "root"
    root.mkdir()
    make_all(root)
    f = root / ("t." + ext)
    res = run_driver(f"""
        boot(HOME, roots=[{str(root)!r}])
        m = call("read_file", {{"path": {str(f)!r}}})
        q = call("data_lens_query", {{"path": {str(f)!r}, "where": [{{"field": "cur", "op": "eq", "value": "EUR"}}],
                                     "select": ["id", "fee"]}})
        emit({{"m": m, "q": q}})
    """)
    m = json.loads(res["m"])
    assert m["data_lens_map"] is True and m["data_lens"]["record_count"] == 3000 and "content" not in m
    assert m["data_lens"]["format"] == ext and m["data_lens"]["coverage"] == "full"
    q = json.loads(res["q"])
    assert [r["values"] for r in q["rows"]] == [{"id": "1500", "fee": "9999.99"}]     # record in the MIDDLE
    assert q["fingerprint"] == m["data_lens"]["fingerprint"]


PASS_THROUGH = """
    import json
    boot(HOME, roots=[{root!r}])
    from tools.registry import registry
    out = {{}}
    def raw(args, task):
        # baseline = the built-in handler called directly (no middleware, no plugin involvement)
        return registry.dispatch("read_file", dict(args), task_id=task)
    cases = {cases}
    for i, (name, args) in enumerate(cases.items()):
        via_chain = call("read_file", args, task_id="chain-%d" % i)
        baseline = raw(args, "base-%d" % i)
        out[name] = {{"same": via_chain == baseline, "map": '"data_lens_map"' in via_chain, "len": len(via_chain)}}
    emit(out)
"""


def test_pass_through_cases_return_exact_builtin_result(tmp_path, run_driver):
    root = tmp_path / "root"
    outside = tmp_path / "outside"
    root.mkdir()
    outside.mkdir()
    make_all(root)
    make_csv(outside / "o.csv")                                     # large, supported, but outside the roots
    (root / "small.csv").write_text("a,b\n1,2\n")
    (root / "big.txt").write_text("line of text number one\n" * 20000)
    (root / "obj.json").write_text(json.dumps({"rows": big_rows(3000)}))      # unsupported JSON shape, large
    (root / "bad.jsonl").write_text("\n".join(json.dumps(r) for r in big_rows(1000)) + "\nnot json\n")
    (root / "rel.csv").write_text("x\n")
    cases = {
        "offset_only": {"path": str(root / "t.csv"), "offset": 1},
        "limit_only": {"path": str(root / "t.csv"), "limit": 200},
        "offset_limit": {"path": str(root / "t.json"), "offset": 5, "limit": 20},
        "small_file": {"path": str(root / "small.csv")},
        "other_ext": {"path": str(root / "big.txt")},
        "outside_root": {"path": str(outside / "o.csv")},
        "unsupported_json_shape": {"path": str(root / "obj.json")},
        "invalid_jsonl": {"path": str(root / "bad.jsonl")},
        "relative_path": {"path": "rel.csv"},
        "missing": {"path": str(root / "nope.csv")},
        "directory": {"path": str(root)},
    }
    res = run_driver(PASS_THROUGH.format(root=str(root), cases=repr(cases)))
    for name, r in res.items():
        assert r["same"] and not r["map"], name


def test_explicit_pagination_after_map_returns_raw_lines(tmp_path, run_driver):
    root = tmp_path / "root"
    root.mkdir()
    make_all(root)
    f = str(root / "t.csv")
    res = run_driver(f"""
        boot(HOME, roots=[{str(root)!r}])
        first = call("read_file", {{"path": {f!r}}})
        again = call("read_file", {{"path": {f!r}}})                       # exact repeat: Hermes dedup stub, untouched
        raw = call("read_file", {{"path": {f!r}, "offset": 1, "limit": 5}})  # documented escape
        emit({{"first": first[:60], "again": again, "raw": raw}})
    """)
    assert res["first"].startswith('{"data_lens_map":true')
    assert json.loads(res["again"]).get("dedup") is True                 # built-in stub passes through unchanged
    raw = json.loads(res["raw"])
    assert raw["content"].startswith("1|id,cur,fee,pad") and "data_lens" not in raw


def test_refused_or_failed_builtin_read_is_unchanged_and_plugin_never_opens_it(tmp_path, run_driver):
    if os.geteuid() == 0:
        pytest.skip("permission-based refusal needs a non-root user")
    root = tmp_path / "root"
    root.mkdir()
    make_all(root)
    locked = root / "locked.csv"
    locked.write_bytes((root / "t.csv").read_bytes())
    locked.chmod(0)
    res = run_driver(f"""
        import os
        boot(HOME, roots=[{str(root)!r}])
        opened = []
        real_open = os.open
        def spy(path, *a, **k):
            opened.append(str(path))
            return real_open(path, *a, **k)
        os.open = spy        # instrumentation only: records, then delegates to the real os.open
        import model_tools   # registers the built-in tools
        from tools.registry import registry
        base = registry.dispatch("read_file", {{"path": {str(locked)!r}}}, task_id="b")
        chain = call("read_file", {{"path": {str(locked)!r}}}, task_id="c")
        emit({{"same": base == chain, "chain": chain, "opened": [p for p in opened if "locked.csv" in p]}})
    """)
    assert json.loads(res["chain"]).get("error")
    assert res["same"] and res["opened"] == []      # the plugin did not touch a file the built-in read failed on
    locked.chmod(0o600)


def test_denied_hermes_internal_path_returns_builtin_error(tmp_path, run_driver):
    root = tmp_path / "root"
    root.mkdir()
    res = run_driver(f"""
        import os
        boot(HOME, roots=[{str(root)!r}, HOME])           # deliberately wide: the Hermes home itself is a root
        d = os.path.join(HOME, "skills", ".hub")
        os.makedirs(d, exist_ok=True)
        p = os.path.join(d, "catalog.csv")
        with open(p, "w") as f:
            f.write("a,b\\n" + "1,2\\n" * 40000)
        base = call("read_file", {{"path": p}}, task_id="x")
        emit({{"r": base}})
    """)
    assert "internal Hermes cache" in json.loads(res["r"])["error"]      # built-in refusal wins; no map


def test_middleware_calls_next_once_and_tools_identity_untouched(tmp_path, run_driver):
    root = tmp_path / "root"
    root.mkdir()
    make_all(root)
    res = run_driver(f"""
        from tools.registry import registry
        import model_tools
        before = registry.get_entry("read_file")
        sig_before = (id(before.handler), json.dumps(before.schema, sort_keys=True)) if False else (id(before.handler), repr(before.schema))
        mgr = boot(HOME, roots=[{str(root)!r}])
        after = registry.get_entry("read_file")
        sig_after = (id(after.handler), repr(after.schema))
        # wrap the registry handler to count BASE executions per chain call (instrumentation, delegating)
        calls = []
        orig = after.handler
        after.handler = lambda args, **kw: (calls.append(dict(args)), orig(args, **kw))[1]
        r = call("read_file", {{"path": {str(root / "t.csv")!r}}})
        after.handler = orig
        emit({{"same_identity": sig_before == sig_after, "base_calls": len(calls), "is_map": r.startswith('{{"data_lens_map"'),
              "mw": mgr._middleware.get("tool_execution") and len(mgr._middleware["tool_execution"])}})
    """.replace("import model_tools", "import json, model_tools"))
    assert res["same_identity"] is True and res["base_calls"] == 1 and res["is_map"] and res["mw"] == 1


def test_agent_loop_composition_runs_middleware_once(tmp_path, run_driver):
    root = tmp_path / "root"
    root.mkdir()
    make_all(root)
    res = run_driver(f"""
        boot(HOME, roots=[{str(root)!r}])
        import model_tools
        from hermes_cli.middleware import run_tool_execution_middleware
        from hermes_cli.plugins import get_plugin_manager
        mgr = get_plugin_manager()
        cb = mgr._middleware["tool_execution"][0]
        seen = []
        def counting(**kw):
            seen.append(kw["tool_name"])
            return cb(**kw)
        mgr._middleware["tool_execution"][0] = counting
        args = {{"path": {str(root / "t.jsonl")!r}}}
        # the shape tool_executor uses: outer middleware wraps an inner call that SKIPS middleware
        out = run_tool_execution_middleware("read_file", args,
            lambda a: model_tools.handle_function_call("read_file", a, task_id="agent", skip_tool_execution_middleware=True),
            original_args=args, task_id="agent")
        emit({{"seen": seen, "map": out.startswith('{{"data_lens_map"')}})
    """)
    assert res == {"seen": ["read_file"], "map": True}


def test_parallel_dispatch_from_threads(tmp_path, run_driver):
    root = tmp_path / "root"
    root.mkdir()
    make_all(root)
    res = run_driver(f"""
        import threading
        boot(HOME, roots=[{str(root)!r}])
        outs, errs = [], []
        def work(i):
            try:
                ext = ["csv", "json", "jsonl"][i % 3]
                p = {str(root)!r} + "/t." + ext
                m = call("read_file", {{"path": p}}, task_id="th%d" % i)
                q = call("data_lens_query", {{"path": p, "group_by": ["cur"], "aggregates": [{{"fn": "sum", "field": "fee", "as": "s"}}]}}, task_id="th%d" % i)
                outs.append((ext, json.loads(m)["data_lens"]["record_count"], {{g["key"][0]: g["aggregates"]["s"] for g in json.loads(q)["groups"]}}))
            except Exception as e:
                errs.append(repr(e))
        ts = [threading.Thread(target=work, args=(i,)) for i in range(12)]
        [t.start() for t in ts]; [t.join() for t in ts]
        emit({{"outs": outs, "errs": errs}})
    """.replace("import threading", "import json, threading"))
    assert res["errs"] == [] and len(res["outs"]) == 12
    assert all(n == 3000 and s == {"EUR": "9999.99", "USD": "299.90"} for _e, n, s in res["outs"])


def test_config_driven_settings_and_default_is_inert(tmp_path, run_driver):
    root = tmp_path / "root"
    root.mkdir()
    make_all(root)
    f = str(root / "t.csv")
    res = run_driver(f"""
        boot(HOME, roots=[])                           # default: no roots
        a = call("read_file", {{"path": {f!r}}}, task_id="a")
        qa = call("data_lens_query", {{"path": {f!r}, "aggregates": [{{"fn": "count"}}]}}, task_id="a")
        boot(HOME, roots=[{str(root)!r}], settings={{"host_local": False}})
        b = call("read_file", {{"path": {f!r}}}, task_id="b")
        boot(HOME, roots=[{str(root)!r}], settings={{"intercept_read_file": False}})
        c = call("read_file", {{"path": {f!r}}}, task_id="c")
        qc = call("data_lens_query", {{"path": {f!r}, "aggregates": [{{"fn": "count"}}]}}, task_id="c")
        boot(HOME, roots=[{str(root)!r}], settings={{"limits": {{"max_rows": 100}}}})
        d = call("read_file", {{"path": {f!r}}}, task_id="d")
        qd = call("data_lens_query", {{"path": {f!r}, "aggregates": [{{"fn": "count"}}]}}, task_id="d")
        boot(HOME, roots=[{str(root)!r}], settings={{"min_bytes": 10**9, "limits": {{"min_bytes": 10**9}}}})
        e = call("read_file", {{"path": {f!r}}}, task_id="e")
        emit({{"a_map": "data_lens_map" in a, "qa": qa, "b_map": "data_lens_map" in b, "c_map": "data_lens_map" in c,
              "qc_ok": '"ok":true' in qc, "d_map": "data_lens_map" in d, "qd": qd, "e_map": "data_lens_map" in e}})
    """)
    assert res["a_map"] is False and json.loads(res["qa"])["error"]["code"] == "roots_not_configured"
    assert res["b_map"] is False                       # host_local not opted in
    assert res["c_map"] is False and res["qc_ok"]      # interception off; tools still work
    assert res["d_map"] is False and json.loads(res["qd"])["error"]["code"] == "row_limit_exceeded"
    assert res["e_map"] is False                       # below the configured min_bytes


def test_unit_middleware_contract_with_counting_next_call(L, env):
    """Direct middleware invocation: next_call exactly once, args object passed through untouched, any
    plugin-side exception returns the downstream result object itself."""
    import json as _j
    p = env.root / "t.csv"
    rows = [[str(i), "x" * 40] for i in range(6000)]
    from helpers import write_csv
    write_csv(p, ["id", "pad"], rows)
    cfg = {"roots": [str(env.root)], "host_local": True}
    mw = L.middleware.make_middleware(lambda k, d=None: cfg.get(k, d))
    size = p.stat().st_size
    base = _j.dumps({"content": "1|id,pad\n", "total_lines": 6001, "file_size": size, "truncated": True})
    for name, args, result, expect_map in [
        ("ok", {"path": str(p)}, base, True),
        ("other_tool", {"path": str(p)}, base, False),
        ("explicit_offset", {"path": str(p), "offset": 1}, base, False),
        ("error_result", {"path": str(p)}, _j.dumps({"error": "x", "content": "c", "truncated": True, "file_size": size}), False),
        ("dedup_stub", {"path": str(p)}, _j.dumps({"status": "unchanged", "dedup": True, "content_returned": False}), False),
        ("binary", {"path": str(p)}, _j.dumps({"content": "", "truncated": True, "is_binary": True, "file_size": size}), False),
        ("not_truncated", {"path": str(p)}, _j.dumps({"content": "c", "truncated": False, "file_size": size}), False),
        ("size_mismatch", {"path": str(p)}, _j.dumps({"content": "c", "truncated": True, "file_size": size + 1}), False),
        ("not_json", {"path": str(p)}, "plain text", False),
        ("non_str", {"path": str(p)}, {"content": "x"}, False),
        ("args_not_dict", "weird", base, False),
    ]:
        calls = []

        def nxt(a, _r=result, _c=calls):
            _c.append(a)
            return _r
        tool = "other" if name == "other_tool" else "read_file"
        out = mw(tool_name=tool, args=args, next_call=nxt, original_args=args, task_id="t")
        assert len(calls) == 1 and calls[0] is args, name
        assert (isinstance(out, str) and out.startswith('{"data_lens_map"')) is expect_map, name
        if not expect_map:
            assert out is result, name
    # plugin-side failure (settings reader explodes) => downstream result returned untouched
    boom = L.middleware.make_middleware(lambda k, d=None: (_ for _ in ()).throw(RuntimeError("boom")))
    assert boom(tool_name="read_file", args={"path": str(p)}, next_call=lambda a: base) is base
    # a downstream exception is NOT swallowed (the chain owns that semantics)
    def raising(a):
        raise ValueError("downstream")
    with pytest.raises(ValueError):
        mw(tool_name="read_file", args={"path": str(p)}, next_call=raising)

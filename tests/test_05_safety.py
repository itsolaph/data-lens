"""Slice 5: roots, file types, protected names, races, fingerprints, no side effects."""
import hashlib
import os
import stat
import threading
import time
from pathlib import Path

import pytest

from helpers import write_csv, write_json


def sha(p):
    return hashlib.sha256(Path(p).read_bytes()).hexdigest()


def snapshot(root):
    return sorted((str(p), p.stat().st_mtime_ns, p.stat().st_size) for p in Path(root).rglob("*"))


def test_roots_default_empty_fails_closed(env, L):
    p = write_csv(env.root / "a.csv", ["a"], [["1"]])
    s = L.core.Settings(roots=(), host_local=True)
    assert env.err(env.profile, p, s).code == "roots_not_configured"
    assert env.err(env.query, p, s, aggregates=[{"fn": "count"}]).code == "roots_not_configured"
    assert L.core.load_settings(lambda k, d=None: d).roots == ()           # nothing configured -> nothing readable
    off = L.core.Settings(roots=(str(env.root),), host_local=False)
    assert env.err(env.profile, p, off).code == "host_local_not_enabled"


def test_outside_relative_and_prefix_tricks(env, tmp_path):
    inside = write_csv(env.root / "in.csv", ["a"], [["1"]])
    outside = write_csv(tmp_path / "out.csv", ["a"], [["1"]])
    sibling = tmp_path / "root-evil"
    sibling.mkdir()
    evil = write_csv(sibling / "e.csv", ["a"], [["1"]])
    assert env.err(env.profile, outside).code == "outside_roots"
    assert env.err(env.profile, evil).code == "outside_roots"                # '/root' is not a string prefix match
    assert env.err(env.profile, str(env.root / ".." / "out.csv")).code == "outside_roots"
    assert env.err(env.profile, "in.csv").code == "path_not_absolute"
    assert env.err(env.profile, "~/in.csv").code == "path_not_absolute"
    assert env.err(env.profile, str(env.root)).code == "unsupported_file_type"
    assert env.err(env.profile, str(env.root / "x.txt")).code == "unsupported_file_type"
    assert env.err(env.L.tools.profile_file, 42, env.settings()).code == "invalid_path"   # non-string, not str()-ed
    assert env.err(env.profile, str(env.root) + "/a\x00.csv").code == "invalid_path"
    assert env.profile(inside)["record_count"] == 1


def test_symlink_escape_rejected_and_internal_link_allowed(env, tmp_path):
    outside = write_csv(tmp_path / "secret_data.csv", ["a"], [["1"]])   # name is fine; location is not
    os.symlink(outside, env.root / "link.csv")
    assert env.err(env.profile, env.root / "link.csv").code == "outside_roots"
    d = tmp_path / "outdir"
    d.mkdir()
    write_csv(d / "x.csv", ["a"], [["1"]])
    os.symlink(d, env.root / "dirlink")
    assert env.err(env.profile, env.root / "dirlink" / "x.csv").code == "outside_roots"
    real = write_csv(env.root / "real.csv", ["a"], [["1"]])
    os.symlink(real, env.root / "ok.csv")
    assert env.profile(env.root / "ok.csv")["record_count"] == 1
    os.symlink(real, env.root / "as.txt")
    assert env.err(env.profile, env.root / "as.txt").code == "unsupported_file_type"
    # a link inside the root to a different type cannot launder the extension
    txt = env.root / "plain.txt"
    txt.write_text("a\n1\n")
    os.symlink(txt, env.root / "laundered.csv")
    assert env.err(env.profile, env.root / "laundered.csv").code == "unsupported_file_type"


def test_special_files_never_opened_or_blocked_on(env, monkeypatch):
    fifo = env.root / "pipe.csv"
    os.mkfifo(fifo)
    t0 = time.monotonic()
    assert env.err(env.profile, fifo).code == "not_regular_file"
    assert time.monotonic() - t0 < 2                 # no blocking open on a FIFO
    d = env.root / "dir.csv"
    d.mkdir()
    assert env.err(env.profile, d).code == "not_regular_file"
    import socket
    sock = env.root / "s.csv"
    srv = socket.socket(socket.AF_UNIX)
    try:
        # AF_UNIX limits the address length even when the filesystem accepts the path.
        with monkeypatch.context() as patch:
            patch.chdir(env.root)
            srv.bind(sock.name)
        assert env.err(env.profile, sock).code == "not_regular_file"
    finally:
        srv.close()
    assert env.err(env.profile, env.root / "missing.csv").code == "not_readable"


@pytest.mark.parametrize("name", [".env.csv", ".env", "secrets.csv", "my_password.json", "credentials.json",
                                  "auth.json", "private_key_dump.jsonl", "id_rsa.csv", "client_secret.json"])
def test_protected_names_refused_even_with_wide_root(env, name):
    p = env.root / name
    p.write_text("[]" if name.endswith("json") else "a\n1\n")
    e = env.err(env.profile, p)
    assert e.code in {"path_not_permitted", "unsupported_file_type"}
    if name.endswith((".csv", ".json", ".jsonl")):
        assert e.code == "path_not_permitted"


def test_protected_directories_hermes_state_and_plugin_dir(env, tmp_path, monkeypatch):
    (env.root / ".ssh").mkdir()
    p = write_csv(env.root / ".ssh" / "x.csv", ["a"], [["1"]])
    assert env.err(env.profile, p).code == "path_not_permitted"
    hh = Path(os.environ["HERMES_HOME"])
    hp = write_csv(hh / "data.csv", ["a"], [["1"]])
    wide = env.L.core.Settings(roots=(str(tmp_path),), host_local=True)    # a deliberately wide root
    assert env.err(env.profile, hp, wide).code == "path_not_permitted"
    plugin_state = Path(env.L.source.__file__).parent / "zz_state.csv"
    assert env.err(env.profile, plugin_state, env.L.core.Settings(roots=(str(plugin_state.parent.parent),), host_local=True)).code \
        == "path_not_permitted"
    (env.root / "proc").mkdir()
    assert env.err(env.profile, "/proc/self/environ.csv").code in {"outside_roots", "path_not_permitted", "not_readable"}


def test_credential_like_content_refused_despite_json_extension(env):
    cases = {
        "pem.json": '[{"k": "-----BEGIN RSA PRIVATE KEY-----\\nabc"}]',
        "aws.jsonl": '{"v": "' + "".join(("AK", "IA", "ABCDEFGH", "IJKLMNOP")) + '"}\n',
        "gh.csv": "v\nghp_" + "a" * 36 + "\n",
        "cols.csv": "user,password\nbob,hunter2\n",
        "keys.json": '[{"user": "bob", "api_key": "x"}]',
        "zero.json": "[]",
    }
    for name, body in cases.items():
        (env.root / name).write_text(body)
    for name in list(cases)[:-1]:
        assert env.err(env.profile, env.root / name).code == "credential_like_content", name
        assert env.err(env.query, env.root / name, aggregates=[{"fn": "count"}]).code == "credential_like_content", name
    assert env.profile(env.root / "zero.json")["record_count"] == 0
    (env.root / "keys0.json").write_text('[{"password": 1}]')
    assert env.err(env.query, env.root / "keys0.json", select=["password"]).code == "credential_like_content"


def test_fingerprint_identical_changed_and_explicit_mismatch(env):
    a = write_csv(env.root / "a.csv", ["x"], [["1"], ["2"]])
    b = env.root / "copy.csv"
    b.write_bytes(Path(a).read_bytes())
    fa, fb = env.profile(a)["fingerprint"], env.profile(b)["fingerprint"]
    assert fa == fb == "sha256:" + sha(a)
    r = env.query(a, expect_fingerprint=fa, aggregates=[{"fn": "count"}])
    assert r["fingerprint"] == fa
    Path(a).write_text("x\n1\n2\n3\n")                       # source changes between profile and query
    e = env.err(env.query, a, expect_fingerprint=fa, aggregates=[{"fn": "count"}])
    assert e.code == "fingerprint_mismatch" and e.extra["expected"] == fa and e.extra["current"] != fa
    again = env.query(a, aggregates=[{"fn": "count"}])      # no expectation: fresh read, new fingerprint, new data
    assert again["groups"][0]["aggregates"]["count"] == 3 and again["fingerprint"] == e.extra["current"]
    assert env.err(env.query, a, expect_fingerprint=5, aggregates=[{"fn": "count"}]).code == "invalid_query"


def test_mutation_during_read_detected_by_fault_injection(env, monkeypatch):
    """INJECTED fault: os.read appends to the file right after the first read returns."""
    p = Path(write_csv(env.root / "m.csv", ["x"], [[str(i)] for i in range(100)]))
    real = os.read
    state = {"n": 0}

    def hostile(fd, n):
        out = real(fd, n)
        state["n"] += 1
        if state["n"] == 1:
            with open(p, "ab") as f:
                f.write(b"999\n")
        return out

    with monkeypatch.context() as patch:
        patch.setattr(env.L.source.os, "read", hostile)
        assert env.err(env.profile, p).code == "file_changed_during_read"
    assert env.profile(p)["record_count"] == 101         # next call reads the new content consistently


def test_swap_to_other_inode_during_read_detected(env, monkeypatch):
    p = Path(write_csv(env.root / "s.csv", ["x"], [["1"], ["2"]]))
    other = Path(write_csv(env.root / "other.csv", ["x"], [["7"], ["8"], ["9"]]))
    real = os.read

    done = []

    def hostile(fd, n):
        out = real(fd, n)
        if not done:
            done.append(1)
            os.replace(other, p)   # INJECTED atomic replace: same path, new inode
        return out

    monkeypatch.setattr(env.L.source.os, "read", hostile)
    assert env.err(env.profile, p).code == "file_changed_during_read"


def test_source_files_unchanged_and_no_writes(env, tmp_path):
    p = write_csv(env.root / "u.csv", ["a", "b"], [[str(i), "0.1"] for i in range(300)])
    q = write_json(env.root / "u.json", [{"a": i} for i in range(300)])
    env.profile(p)   # warm-up: Hermes' own config loader bootstraps an empty HERMES_HOME (SOUL.md, dirs) on first use
    before = (sha(p), sha(q), snapshot(env.root), snapshot(os.environ["HERMES_HOME"]))
    for f in (p, q):
        env.profile(f)
        env.query(f, aggregates=[{"fn": "count"}])
        env.query(f, select=["a"])
    assert before == (sha(p), sha(q), snapshot(env.root), snapshot(os.environ["HERMES_HOME"]))


def test_concurrent_calls_share_no_state(env):
    p = write_csv(env.root / "c.csv", ["g", "v"], [[str(i % 5), "0.1"] for i in range(2000)])
    results, errors = [], []

    def work(i):
        try:
            for _ in range(5):
                results.append(env.query(p, group_by=["g"], aggregates=[{"fn": "sum", "field": "v", "as": "s"}]))
        except Exception as ex:   # pragma: no cover
            errors.append(ex)

    ts = [threading.Thread(target=work, args=(i,)) for i in range(8)]
    [t.start() for t in ts]
    [t.join() for t in ts]
    assert not errors and len(results) == 40 and all(r == results[0] for r in results)
    assert {g["key"][0]: g["aggregates"]["s"] for g in results[0]["groups"]} == {str(k): "40.0" for k in range(5)}

import json
import subprocess
import sys
import textwrap
from pathlib import Path

import pytest

TESTS = Path(__file__).resolve().parent
REPO = TESTS.parent
MARK = "@@RESULT@@"
sys.path.insert(0, str(TESTS))


@pytest.fixture
def run_driver(tmp_path):
    """Run driver code in a fresh interpreter (real Hermes, isolated HERMES_HOME)."""
    def _run(code, timeout=180):
        home = tmp_path / "hermes_home"
        drv = tmp_path / "driver.py"
        drv.write_text("import sys\nsys.path.insert(0, %r)\nfrom _runtime import *\nHOME = %r\n" % (str(TESTS), str(home))
                       + textwrap.dedent(code))
        proc = subprocess.run([sys.executable, str(drv)], capture_output=True, text=True, timeout=timeout,
                              cwd=str(tmp_path), env={"PATH": "/usr/bin:/bin", "HOME": str(tmp_path / "fakehome")})
        out = proc.stdout
        if MARK not in out:
            raise AssertionError(f"driver failed rc={proc.returncode}\nSTDOUT:{out[-3000:]}\nSTDERR:{proc.stderr[-3000:]}")
        return json.loads(out.rsplit(MARK, 1)[1].strip().splitlines()[0])
    return _run


@pytest.fixture(autouse=True)
def _isolated_home(tmp_path, monkeypatch):
    """Unit tests must never read a real Hermes profile."""
    hh = tmp_path / "hermes_home_unit"
    hh.mkdir()
    monkeypatch.setenv("HERMES_HOME", str(hh))
    monkeypatch.setenv("HOME", str(tmp_path / "fakehome"))
    monkeypatch.delenv("TERMINAL_ENV", raising=False)


@pytest.fixture
def L():
    from helpers import load_pkg
    return load_pkg()


@pytest.fixture
def env(tmp_path, L):
    """root dir + settings factory + query/profile helpers."""
    root = tmp_path / "root"
    root.mkdir()

    class Env:
        pass

    e = Env()
    e.root = root
    e.L = L

    def settings(**limits):
        lim, _w = L.core.build_limits(limits) if limits else (L.core.Limits(), ())
        return L.core.Settings(roots=(str(root),), host_local=True, limits=lim)

    e.settings = settings
    e.query = lambda path, _s=None, **spec: L.tools.query_file({"path": str(path), **spec}, _s or settings())
    e.profile = lambda path, _s=None: L.tools.profile_file(str(path), _s or settings())

    def err(fn, *a, **k):
        try:
            fn(*a, **k)
        except L.core.LensError as ex:
            return ex
        raise AssertionError("expected LensError")

    e.err = err
    return e

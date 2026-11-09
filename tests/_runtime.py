"""Test-side runtime bootstrap: real Hermes PluginManager discovery in an isolated HERMES_HOME.

Imported by driver scripts that run in a *subprocess* (global plugin/registry state is process-wide).
No mocks of plugin logic: the plugin directory is copied verbatim into <home>/plugins/data-lens.
"""
from __future__ import annotations

import json
import os
import shutil
import sys
from pathlib import Path

REPO = Path(__file__).resolve().parent.parent
COPY = ("plugin.yaml", "__init__.py", "LICENSE")


def boot(home, roots=(), settings=None, enable=True):
    home = Path(home)
    (home / "plugins").mkdir(parents=True, exist_ok=True)
    dest = home / "plugins" / "data-lens"
    if dest.exists():
        shutil.rmtree(dest)
    dest.mkdir()
    for name in COPY:
        if (REPO / name).exists():
            shutil.copy2(REPO / name, dest / name)
    for p in REPO.glob("data_lens_*.py"):
        shutil.copy2(p, dest / p.name)
    st = {"roots": [str(r) for r in roots], "host_local": True}
    st.update(settings or {})
    lines = ["plugins:"]
    if enable:
        lines.append("  enabled: [data-lens]")
    lines += ["  entries:", "    data-lens:", "      settings:"]
    for k, v in st.items():
        lines.append(f"        {k}: {json.dumps(v)}")
    (home / "config.yaml").write_text("\n".join(lines) + "\n")
    os.environ["HERMES_HOME"] = str(home)
    os.environ.pop("TERMINAL_ENV", None)
    from hermes_cli.plugins import get_plugin_manager
    mgr = get_plugin_manager()
    mgr.discover_and_load(force=True)
    return mgr


def call(tool, args, task_id="t1"):
    import model_tools
    return model_tools.handle_function_call(tool, dict(args), task_id=task_id, session_id="s1")


def emit(obj):
    sys.stdout.write("\n@@RESULT@@" + json.dumps(obj, default=str) + "\n")

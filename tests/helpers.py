"""Shared test helpers (in-process unit tests load the plugin as a real package)."""
import csv
import importlib
import importlib.util
import json
import sys
from pathlib import Path
from types import SimpleNamespace

REPO = Path(__file__).resolve().parent.parent


def load_pkg():
    if "data_lens_pkg" not in sys.modules:
        spec = importlib.util.spec_from_file_location(
            "data_lens_pkg", REPO / "__init__.py", submodule_search_locations=[str(REPO)])
        mod = importlib.util.module_from_spec(spec)
        sys.modules["data_lens_pkg"] = mod
        spec.loader.exec_module(mod)
    g = lambda n: importlib.import_module("data_lens_pkg." + n)  # noqa: E731
    return SimpleNamespace(core=g("data_lens_core"), source=g("data_lens_source"), scan=g("data_lens_scan"),
                           profile=g("data_lens_profile"), query=g("data_lens_query"), tools=g("data_lens_tools"),
                           middleware=g("data_lens_middleware"), pkg=sys.modules["data_lens_pkg"])


def write_csv(path, header, rows, bom=False):
    with open(path, "w", newline="", encoding="utf-8-sig" if bom else "utf-8") as f:
        w = csv.writer(f)
        w.writerow(header)
        w.writerows(rows)
    return str(path)


def write_jsonl(path, records, raw_lines=None):
    lines = raw_lines if raw_lines is not None else [json.dumps(r) for r in records]
    Path(path).write_text("\n".join(lines) + "\n", encoding="utf-8")
    return str(path)


def write_json(path, records):
    Path(path).write_text(json.dumps(records), encoding="utf-8")
    return str(path)

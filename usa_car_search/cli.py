"""Console entry point for the legacy script module."""

from __future__ import annotations

import importlib.util
from pathlib import Path


def _load_script_module():
    root = Path(__file__).resolve().parents[1]
    script = root / "usa-car-search.py"
    spec = importlib.util.spec_from_file_location("usa_car_search_script", script)
    if spec is None or spec.loader is None:
        raise RuntimeError(f"Unable to load {script}")
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


def main():
    module = _load_script_module()
    return module.main()


if __name__ == "__main__":
    main()
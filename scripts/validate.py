#!/usr/bin/env python3
"""validate.py — Standalone validation command (Phase 2 of NashSU refactor)

Separated from ingest.py for independent, detailed quality checks.
Can be run offline or in CI/CD pipelines.

Usage:
    python3 validate.py <source_slug>
    SOURCE_SLUG=file validate.py
"""

import json
import os
import sys
from pathlib import Path

_script_dir = Path(__file__).resolve().parent
sys.path.insert(0, str(_script_dir))

from _core import Config, detect_runtime_dir, load_cache
from validate_ingest import main as run_validation


def main():
    """Run validation as standalone command"""
    project_root = Path(os.environ.get("IMPROVED_WIKI_ROOT", os.getcwd()))
    source_slug = os.environ.get("SOURCE_SLUG", "")

    print(f"📋 Validation Command (Standalone)")
    print(f"  Project: {project_root}")
    if source_slug:
        print(f"  Source: {source_slug}")
    print()

    # Run the existing validation
    return run_validation()


if __name__ == "__main__":
    exit_code = main()
    sys.exit(exit_code if exit_code else 0)

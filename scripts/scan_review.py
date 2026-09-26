#!/usr/bin/env python3
"""Inspect saved scan evidence, or request a non-destructive region second opinion."""
from __future__ import annotations

import argparse
import json
import os
from pathlib import Path

from _config import Config
from _scan_evidence import review_regions


def main(argv=None):
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--root', type=Path, required=True)
    parser.add_argument('--manifest', type=Path, required=True)
    parser.add_argument('--region', action='append', default=[], help='Region ID, e.g. r0004')
    parser.add_argument('--apply', action='store_true', help='Call configured VLM; never change OCR')
    parser.add_argument('--limit', type=int, default=8)
    args = parser.parse_args(argv)
    os.environ['IMPROVED_WIKI_ROOT'] = str(args.root.resolve())
    config = Config.from_env()
    path = args.manifest.resolve()
    if not path.is_relative_to((config.runtime_dir / 'scan-evidence').resolve()):
        parser.error('manifest must be inside this project runtime/scan-evidence')
    if args.limit < 1:
        parser.error('--limit must be positive')
    if args.apply:
        from _maintenance_lock import maintenance_write_lock
        with maintenance_write_lock(config):
            result = review_regions(path, config, selected=set(args.region), limit=args.limit)
    else:
        result = json.loads(path.read_text())
    print(json.dumps(result, ensure_ascii=False, indent=2))
    if args.apply and any(item['status'] in ('unavailable', 'deferred')
                          for item in result['regions'].values()):
        return 2
    return 0


if __name__ == '__main__':
    raise SystemExit(main())

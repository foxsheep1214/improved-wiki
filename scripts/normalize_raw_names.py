#!/usr/bin/env python3
"""Generic raw/ file name normalizer for any wiki project.

Reads <project>/raw/NAMING.md and extracts machine-readable rules from the
```yaml rules``` block.  Checks all (or recent) files against those rules.
In --fix mode, renames files that can be automatically corrected.

Usage:
  # Auto-detect project from CWD
  python3 normalize_raw_names.py --check
  python3 normalize_raw_names.py --fix --recent 30

  # Explicit project
  python3 normalize_raw_names.py --project ~/Documents/知识库/HardwareWiki --check
"""

import argparse
import os
import re
import sys
import time as _time
from pathlib import Path
from typing import Optional, List, Dict, Tuple

SCRIPT_DIR = Path(__file__).resolve().parent


# ── Lightweight YAML block parser (no PyYAML dependency) ────────

def _parse_yaml_block(text: str) -> dict:
    """Parse the ```yaml rules``` block from NAMING.md."""
    m = re.search(r'```yaml\s*\n(.*?)\n```', text, re.DOTALL)
    if not m:
        return {}
    return _parse_simple_yaml(m.group(1))


def _parse_simple_yaml(text: str) -> dict:
    """Minimal YAML parser for the NAMING.md rules block subset.

    Supports nested dicts, string lists (both ['a', 'b'] and flattened
    'a - b - c' items), and scalar values.
    """
    lines = text.split('\n')
    result: dict = {}
    stack: List[Tuple[int, str, object]] = []

    for line in lines:
        stripped = line.rstrip()
        if not stripped or stripped.startswith('#'):
            continue

        indent = len(line) - len(line.lstrip())
        key, sep, val = stripped.partition(':')
        key = key.strip()
        if not sep:
            continue
        val = val.strip()

        # Pop stack to find parent
        while stack and stack[-1][0] >= indent:
            stack.pop()
        parent = stack[-1][2] if stack else result

        if val == '':
            parent[key] = {}
            stack.append((indent, key, parent[key]))
        elif val.startswith('- '):
            items = [v.strip().strip("'\"") for v in val.split(' - ')
                     if v.strip().strip("'\"")]
            if key in parent and isinstance(parent[key], list):
                parent[key].extend(items)
            else:
                parent[key] = items
        else:
            v = val.strip("'\"")
            if v.isdigit():
                v = int(v)
            elif v in ('true', 'false'):
                v = (v == 'true')
            parent[key] = v

    return result


# ── Rule resolution ─────────────────────────────────────────────

def _resolve_rule(rules: dict, folder_type: str) -> dict:
    """Resolve a rule, following 'extends' chains."""
    if folder_type not in rules:
        return {}
    rule = dict(rules[folder_type])
    if 'extends' in rule:
        parent = _resolve_rule(rules, rule['extends'])
        for k, v in parent.items():
            if k not in rule:
                rule[k] = v
    return rule


def _flatten_prefixes(vendor_prefixes: dict) -> Dict[str, str]:
    """Flatten vendor_prefixes into {lower_prefix: vendor_name}."""
    result: Dict[str, str] = {}
    for vendor, groups in vendor_prefixes.items():
        for group in groups:
            if isinstance(group, list):
                for prefix in group:
                    prefix = str(prefix).strip().strip("'\"")
                    if prefix:
                        result[prefix.lower()] = vendor
            else:
                for prefix in str(group).split():
                    prefix = prefix.strip().strip("'\"")
                    if prefix:
                        result[prefix.lower()] = vendor
    return result


def _infer_vendor(part_number: str, prefix_map: Dict[str, str]) -> Optional[str]:
    """Infer vendor from part number prefix. Longest match (4→3→2 chars) wins."""
    pn = part_number.upper().replace('-', '').replace('_', '')
    for length in (4, 3, 2):
        prefix = pn[:length].lower()
        if prefix in prefix_map:
            return prefix_map[prefix]
    return None


# ── Check & Fix ─────────────────────────────────────────────────

def _check_rule(filepath: Path, rule: dict, vendors: List[str],
                prefix_map: Dict[str, str]) -> List[str]:
    """Check a file against its folder's naming rule."""
    stem = filepath.stem
    issues: List[str] = []
    min_parts = rule.get('min_parts', 1)
    parts = stem.split(' - ')

    if len(parts) < min_parts:
        pattern = rule.get('pattern', '?')
        issues.append(f"格式不符合（应为「{pattern}」）")
        return issues

    vf = rule.get('vendor_field')
    if vf is not None and vf < len(parts):
        if parts[vf] not in vendors:
            issues.append(f"未识别的 Vendor：「{parts[vf]}」")

    yf = rule.get('year_field')
    if yf is not None and yf < len(parts):
        if not re.match(r'^\d{4}$', parts[yf]):
            issues.append(f"年份格式不正确：「{parts[yf]}」")

    df = rule.get('date_field')
    if df is not None and df < len(parts):
        if not re.match(r'^\d{4}-\d{2}-\d{2}$', parts[df]):
            issues.append(f"日期格式不正确：「{parts[df]}」")

    return issues


def _fix_file(filepath: Path, rule: dict, vendors: List[str],
              prefix_map: Dict[str, str], dry_run: bool = True) -> Optional[Tuple[Path, str]]:
    """Try to fix a filename. Only handles 'add vendor prefix' for now."""
    stem = filepath.stem

    if rule.get('vendor_field') != 0:
        return None

    if ' - ' in stem:
        parts = stem.split(' - ', 1)
        if parts[0] in vendors:
            return None

    vendor = _infer_vendor(stem, prefix_map)
    if vendor is None:
        return None

    new_stem = f"{vendor} - {stem}"
    new_path = filepath.with_name(new_stem + filepath.suffix)
    if dry_run:
        return (new_path, f"推断 Vendor={vendor}")
    else:
        filepath.rename(new_path)
        return (new_path, f"已改名：Vendor={vendor}")


# ── Scanner ─────────────────────────────────────────────────────

def scan_raw(raw_root: Path, rules: dict, check: bool = True, fix: bool = False,
             verbose: bool = False, recent_minutes: Optional[int] = None) -> Dict:
    """Scan raw/ files against parsed rules from NAMING.md."""
    results = {"ok": 0, "issues": 0, "fixed": 0, "unfixable": 0}
    cutoff = _time.time() - (recent_minutes * 60) if recent_minutes else 0
    files_skipped = 0
    vendors = rules.get('vendors', [])
    prefix_map = _flatten_prefixes(rules.get('vendor_prefixes', {}))
    folder_rules = rules.get('rules', {})

    for folder_name, _rule_def in folder_rules.items():
        folder = raw_root / folder_name
        if not folder.exists():
            continue

        rule = _resolve_rule(folder_rules, folder_name)

        for filepath in sorted(folder.rglob('*')):
            if not filepath.is_file():
                continue
            if filepath.suffix.lower() not in ('.pdf',):
                continue
            if filepath.name.startswith('.'):
                continue
            if filepath.name == 'NAMING.md':
                continue

            if recent_minutes:
                try:
                    if filepath.stat().st_mtime < cutoff:
                        files_skipped += 1
                        continue
                except OSError:
                    continue

            rel = filepath.relative_to(raw_root)
            issues = _check_rule(filepath, rule, vendors, prefix_map)

            if not issues:
                results["ok"] += 1
                if verbose:
                    print(f"  ✅ {rel}")
                continue

            results["issues"] += 1
            print(f"  ❌ {rel}")
            for issue in issues:
                print(f"      {issue}")

            if fix:
                outcome = _fix_file(filepath, rule, vendors, prefix_map, dry_run=False)
                if outcome:
                    new_path, reason = outcome
                    print(f"      → {new_path.relative_to(raw_root)}  ({reason})")
                    results["fixed"] += 1
                else:
                    print(f"      ⚠️  无法自动修正")
                    results["unfixable"] += 1

    if recent_minutes and files_skipped > 0:
        print(f"  ⏭️  跳过 {files_skipped} 个旧文件（超过 {recent_minutes} 分钟前修改）")

    return results


# ── CLI ─────────────────────────────────────────────────────────

def find_project_root() -> Optional[Path]:
    """Walk up from CWD to find a project with raw/NAMING.md."""
    cwd = Path.cwd()
    for p in [cwd] + list(cwd.parents):
        if (p / 'raw' / 'NAMING.md').exists():
            return p
    return None


def main():
    parser = argparse.ArgumentParser(
        description="Normalize raw/ file names for any wiki project")
    parser.add_argument('--project', type=Path,
                        help='Wiki project root (auto-detected from CWD if omitted)')
    parser.add_argument('--check', action='store_true', default=True)
    parser.add_argument('--fix', action='store_true')
    parser.add_argument('--verbose', '-v', action='store_true')
    parser.add_argument('--recent', type=int, metavar='MINUTES',
                        help='Only check files modified within the last N minutes')
    args = parser.parse_args()

    project_root = args.project or find_project_root()
    if project_root is None:
        print("Error: Could not find raw/NAMING.md. "
              "Run from a wiki project or use --project.")
        return 1

    raw_root = project_root / 'raw'
    naming_md = raw_root / 'NAMING.md'

    if not naming_md.exists():
        print(f"Error: {naming_md} not found. Create it first.")
        return 1

    rules = _parse_yaml_block(naming_md.read_text(encoding='utf-8'))
    if not rules:
        print(f"Error: No ```yaml rules``` block found in {naming_md}")
        return 1

    mode = "🔧 修正" if args.fix else "🔍 检查"
    scope = f"（最近 {args.recent} 分钟内修改的文件）" if args.recent else ""
    print(f"{mode}模式{scope} — {project_root.name}\n")

    results = scan_raw(raw_root, rules, check=True, fix=args.fix,
                       verbose=args.verbose, recent_minutes=args.recent)

    print(f"\n── 结果 ──")
    print(f"  ✅ 符合规范: {results['ok']}")
    print(f"  ❌ 不符合:   {results['issues']}")
    if args.fix:
        print(f"  🔧 已修正:   {results['fixed']}")
        print(f"  ⚠️  无法自动修正: {results['unfixable']}")

    return 0 if results['issues'] == 0 or (args.fix and results['unfixable'] == 0) else 1


if __name__ == '__main__':
    sys.exit(main())

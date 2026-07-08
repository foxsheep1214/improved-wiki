#!/usr/bin/env python3
"""Stage 0.1: raw/ file name normalizer for any wiki project.

The pregate (Stage 0) pre-processing gate. Reads <project>/raw/NAMING.md and
extracts machine-readable rules from the ```yaml rules``` block. Checks all
(or recent) files against those rules. In --fix mode, renames files that can
be automatically corrected.

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


# ── Lightweight YAML block parser (no PyYAML dependency) ────────

def _stage_0_1_parse_yaml_block(text: str) -> dict:
    """Parse the naming-rules ```yaml block from schema.md / NAMING.md.

    schema.md contains several ```yaml fences (e.g. the frontmatter example);
    pick the one that actually holds the rules — identified by a top-level
    ``rules:`` or ``forbidden_chars:`` key — not merely the first fence (which
    used to silently parse the frontmatter block, leaving rules empty and making
    the whole checker a no-op).
    """
    blocks = re.findall(r'```yaml\s*\n(.*?)\n```', text, re.DOTALL)
    for b in blocks:
        if re.search(r'^(rules|forbidden_chars):', b, re.MULTILINE):
            return _stage_0_1_parse_simple_yaml(b)
    return _stage_0_1_parse_simple_yaml(blocks[0]) if blocks else {}


def _stage_0_1_parse_simple_yaml(text: str) -> dict:
    """Minimal YAML parser for the NAMING.md rules block subset.

    Supports nested dicts, scalars, and lists in TWO syntaxes:
      * inline:  ``key: - a - b - c``            (single line, split on ' - ')
      * block:   ``key:\\n  - a\\n  - b``          (one item per line)

    Block lists are the form NAMING.md actually uses; the previous impl
    skipped any line without a colon, silently dropping every ``- item``
    line and leaving ``vendors`` / ``vendor_prefixes`` empty — which made
    every datasheet fail the vendor check with a false "未识别的 Vendor".

    A ``key:`` with an empty value is resolved to a list or dict by
    looking ahead at the next deeper line (``- `` → list, else dict).
    Duplicate top-level keys: last wins (YAML semantics).
    """
    lines = text.split('\n')
    result: dict = {}
    stack: List[Tuple[int, str, object]] = []

    def _split_inline_list(raw: str) -> List[str]:
        raw = raw.strip()
        if raw.startswith('- '):
            raw = raw[2:]  # strip a leading list marker (inline `key: - a - b`)
        return [v.strip().strip("'\"") for v in raw.split(' - ')
                if v.strip().strip("'\"")]

    for idx, line in enumerate(lines):
        stripped = line.rstrip()
        if not stripped or stripped.lstrip().startswith('#'):
            continue
        indent = len(line) - len(line.lstrip())
        content = stripped.lstrip()

        # Block list item: "- value" on its own line (no key/colon).
        # Belongs to the nearest list container on the stack. A single
        # block item may itself carry several ' - '-separated values
        # (e.g. "- ADC - ADS - AFE"), matching the inline-list convention.
        if content.startswith('- '):
            if stack and isinstance(stack[-1][2], list):
                stack[-1][2].extend(_split_inline_list(content[2:]))
            continue

        key, sep, val = content.partition(':')
        key = key.strip()
        if not sep:
            continue
        val = val.strip()

        # Pop stack to find parent
        while stack and stack[-1][0] >= indent:
            stack.pop()
        parent = stack[-1][2] if stack else result

        if val == '':
            # Look ahead: first deeper non-blank line decides list vs dict.
            is_list = False
            for nl in lines[idx + 1:]:
                nls = nl.rstrip()
                if not nls or nls.lstrip().startswith('#'):
                    continue
                if (len(nl) - len(nl.lstrip())) <= indent:
                    break
                is_list = nls.lstrip().startswith('- ')
                break
            container: object = [] if is_list else {}
            parent[key] = container
            stack.append((indent, key, container))
        elif val.startswith('- '):
            items = _split_inline_list(val)
            if key in parent and isinstance(parent[key], list):
                parent[key].extend(items)
            else:
                parent[key] = items
        else:
            v = val.strip("'\"")
            if re.match(r'^-?\d+$', v):
                v = int(v)
            elif v in ('true', 'false'):
                v = (v == 'true')
            parent[key] = v

    return result


# ── Rule resolution ─────────────────────────────────────────────

def _stage_0_1_resolve_rule(rules: dict, folder_type: str) -> dict:
    """Resolve a rule, following 'extends' chains."""
    if folder_type not in rules:
        return {}
    rule = dict(rules[folder_type])
    if 'extends' in rule:
        parent = _stage_0_1_resolve_rule(rules, rule['extends'])
        for k, v in parent.items():
            if k not in rule:
                rule[k] = v
    return rule


def _stage_0_1_flatten_prefixes(vendor_prefixes: dict) -> Dict[str, str]:
    """Flatten vendor_prefixes into {lower_prefix: vendor_name}.

    Robust against malformed YAML: only process list values for each vendor,
    skipping non-list types to prevent silent failures.
    """
    result: Dict[str, str] = {}
    for vendor, groups in vendor_prefixes.items():
        if not isinstance(groups, list):
            continue
        for group in groups:
            if isinstance(group, list):
                for prefix in group:
                    prefix = str(prefix).strip().strip("'\"")
                    if prefix:
                        result[prefix.lower()] = vendor
            elif isinstance(group, str):
                for prefix in group.split():
                    prefix = prefix.strip().strip("'\"")
                    if prefix:
                        result[prefix.lower()] = vendor
    return result


def _stage_0_1_infer_vendor(part_number: str, prefix_map: Dict[str, str]) -> Optional[str]:
    """Infer vendor from part number prefix. Longest match (4→3→2 chars) wins."""
    pn = part_number.upper().replace('-', '').replace('_', '')
    for length in (4, 3, 2):
        prefix = pn[:length].lower()
        if prefix in prefix_map:
            return prefix_map[prefix]
    return None


# ── Check & Fix ─────────────────────────────────────────────────

def _stage_0_1_surname_warnings(author: str) -> List[str]:
    """Heuristic warnings for an author field that should be surname-only.

    Conservative by design — only flags high-confidence violations:
      * explicit multi-author markers ('et al', '等')
      * standalone given-name initials ('Y-M', 'M.', 'J')
      * 3+ words (almost certainly a full name or an author list)

    A 2-word field is NOT flagged: multi-word surnames like 'Ben Salah' are
    valid, and 'Ben Salah' (surname) cannot be reliably told from
    'Hong Zhangjie' (full name) by shape alone.

    CJK-only fields are skipped: the Book rule permits Chinese full names,
    and CJK surname splitting would require a surname dictionary.
    """
    warns: List[str] = []
    a = author.strip()
    if not a or not re.search(r'[A-Za-z]', a):
        return warns
    if re.search(r'\bet al\.?\b', a, re.IGNORECASE):
        warns.append(f"作者段含「et al」，按规则只写第一作者姓氏：「{author}」")
    if '等' in a:
        warns.append(f"作者段含「等」，按规则只写第一作者姓氏：「{author}」")
    # Standalone initials: a single capital letter (optional period), possibly
    # hyphen-chained ('Y-M', 'M.-J'). Matches 'Wu Y-M', 'Smith J.'; does not
    # match 'Ben' / 'Salah' / 'Smith-Jones' (lowercase follows the capital).
    if re.search(r'(?<!\S)[A-Z]\.?(?:-[A-Z]\.?)*(?!\S)', a):
        warns.append(f"作者段含名字缩写，应只保留姓氏：「{author}」")
    words = a.split()
    if len(words) >= 3:
        warns.append(f"作者段含多词（{len(words)} 词），疑似全名或多作者，应只写第一作者姓氏：「{author}」")
    return warns


def _stage_0_1_check_rule(filepath: Path, rule: dict, vendors: List[str],
                prefix_map: Dict[str, str]) -> List[Tuple[str, str]]:
    """Check a file against its folder's naming rule.

    Returns a list of (severity, message) tuples. ``severity`` is ``'error'``
    (a hard violation) or ``'warn'`` (a heuristic suspicion, e.g. the author
    field looks like a full name under a surname-only rule).
    """
    stem = filepath.stem
    issues: List[Tuple[str, str]] = []
    min_parts = rule.get('min_parts', 1)
    parts = stem.split(' - ')

    if len(parts) < min_parts:
        pattern = rule.get('pattern', '?')
        issues.append(("error", f"格式不符合（应为「{pattern}」）"))
        return issues

    vf = rule.get('vendor_field')
    if vf is not None and isinstance(vf, int) and 0 <= vf < len(parts):
        if vendors and parts[vf] not in vendors:
            issues.append(("error", f"未识别的 Vendor：「{parts[vf]}」"))

    yf = rule.get('year_field')
    if yf is not None and isinstance(yf, int) and 0 <= yf < len(parts):
        if not re.match(r'^\d{4}$', parts[yf]):
            issues.append(("error", f"年份格式不正确：「{parts[yf]}」"))

    df = rule.get('date_field')
    if df is not None and isinstance(df, int) and 0 <= df < len(parts):
        if not re.match(r'^\d{4}-\d{2}-\d{2}$', parts[df]):
            issues.append(("error", f"日期格式不正确：「{parts[df]}」"))

    # Author-field heuristic (warn-level): for surname-only rules, flag author
    # segments that look like full names or multi-author lists.
    af = rule.get('author_field')
    if af is not None and isinstance(af, int) and rule.get('surname_only'):
        idx = af if af >= 0 else len(parts) + af
        if 0 <= idx < len(parts):
            for w in _stage_0_1_surname_warnings(parts[idx]):
                issues.append(("warn", w))

    return issues


def _stage_0_1_fix_file(filepath: Path, rule: dict, vendors: List[str],
              prefix_map: Dict[str, str], dry_run: bool = True) -> Optional[Tuple[Path, str]]:
    """Try to fix a filename. Handles 'add vendor prefix' or report unfixable."""
    stem = filepath.stem

    if rule.get('vendor_field') != 0:
        return None

    if ' - ' in stem:
        parts = stem.split(' - ', 1)
        if parts[0] in vendors:
            return None

    vendor = _stage_0_1_infer_vendor(stem, prefix_map)
    if vendor is None:
        return None

    new_stem = f"{vendor} - {stem}"
    new_path = filepath.with_name(new_stem + filepath.suffix)

    if dry_run:
        return (new_path, f"推断 Vendor={vendor}")
    else:
        try:
            if new_path.exists():
                return (filepath, f"ERROR: 目标文件已存在「{new_path.name}」")
            filepath.rename(new_path)
            return (new_path, f"已改名：Vendor={vendor}")
        except OSError as e:
            return (filepath, f"ERROR: 改名失败 — {e}")


# ── Scanner ─────────────────────────────────────────────────────

def stage_0_1_scan_raw(raw_root: Path, rules: dict, check: bool = True, fix: bool = False,
             verbose: bool = False, recent_minutes: Optional[int] = None) -> Dict:
    """Scan raw/ files against parsed rules from NAMING.md."""
    results = {"ok": 0, "issues": 0, "warns": 0, "fixed": 0, "unfixable": 0}
    cutoff = _time.time() - (recent_minutes * 60) if recent_minutes else 0
    files_skipped = 0
    vendors = rules.get('vendors', [])
    prefix_map = _stage_0_1_flatten_prefixes(rules.get('vendor_prefixes', {}))
    folder_rules = rules.get('rules', {})
    # Global forbidden chars (schema-driven, default to commas): a comma in a raw
    # filename gets split by the source-citation renderer → broken [[sources/...]].
    forbidden_chars = rules.get('forbidden_chars') or [',', '，']
    if isinstance(forbidden_chars, str):
        forbidden_chars = [forbidden_chars]

    for folder_name, _rule_def in folder_rules.items():
        folder = raw_root / folder_name
        if not folder.exists():
            continue

        rule = _stage_0_1_resolve_rule(folder_rules, folder_name)

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
            issues = _stage_0_1_check_rule(filepath, rule, vendors, prefix_map)
            for ch in forbidden_chars:
                if ch and ch in filepath.stem:
                    issues.append(("error",
                        f"文件名含禁用字符「{ch}」（逗号会被来源引用按逗号切分书名→断链），改用 ' - '"))
            errors = [m for s, m in issues if s == "error"]
            warns = [m for s, m in issues if s == "warn"]

            if not errors and not warns:
                results["ok"] += 1
                if verbose:
                    print(f"  ✅ {rel}")
                continue

            if errors:
                results["issues"] += 1
                print(f"  ❌ {rel}")
            else:
                results["warns"] += 1
                print(f"  ⚠️  {rel}")
            for m in errors:
                print(f"      {m}")
            for m in warns:
                print(f"      ⚠ {m}")

            if fix and errors:
                outcome = _stage_0_1_fix_file(filepath, rule, vendors, prefix_map, dry_run=False)
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


def stage_0_1_check_file(raw_file: Path, project_root: Path) -> List[str]:
    """Per-file Stage 0.1 naming gate for the ingest pipeline (wired 2026-07-08).

    Returns error strings (empty = compliant or out of scope). Scope mirrors
    ``stage_0_1_scan_raw``: only ``.pdf`` files under a folder that declares a
    rule are checked — so e.g. ``raw/queries/*.md`` deep-research bridge copies
    always pass. Warn-level heuristics do not block.

    Raises ``RuntimeError`` when the project has no parseable naming rules
    (``schema.md`` missing or lacking the ```yaml rules block) — the documented
    Stage 0.1 "draft the rules first" stop, no-silent-fallback aligned.
    """
    schema_md = project_root / 'schema.md'
    if not schema_md.exists():
        raise RuntimeError(
            f"[Stage 0.1] {schema_md} not found — draft the project naming "
            f"rules first (see references/raw-naming-conventions.md).")
    rules = _stage_0_1_parse_yaml_block(schema_md.read_text(encoding='utf-8'))
    if not rules or not rules.get('rules'):
        raise RuntimeError(
            f"[Stage 0.1] no ```yaml naming-rules block in {schema_md} — "
            f"draft it first (see references/raw-naming-conventions.md).")

    if raw_file.suffix.lower() != '.pdf':
        return []
    raw_root = project_root / 'raw'
    try:
        rel = raw_file.resolve().relative_to(raw_root.resolve())
    except ValueError:
        return []
    if len(rel.parts) < 2 or rel.parts[0] not in rules['rules']:
        return []

    vendors_file = raw_root / 'Datasheet' / 'VENDORS.yaml'
    if vendors_file.exists():
        vdata = _stage_0_1_parse_simple_yaml(vendors_file.read_text(encoding='utf-8'))
        if vdata.get('vendors'):
            rules['vendors'] = vdata['vendors']
        if vdata.get('vendor_prefixes'):
            rules['vendor_prefixes'] = vdata['vendor_prefixes']

    rule = _stage_0_1_resolve_rule(rules['rules'], rel.parts[0])
    prefix_map = _stage_0_1_flatten_prefixes(rules.get('vendor_prefixes', {}))
    issues = _stage_0_1_check_rule(raw_file, rule, rules.get('vendors', []), prefix_map)
    forbidden_chars = rules.get('forbidden_chars') or [',', '，']
    if isinstance(forbidden_chars, str):
        forbidden_chars = [forbidden_chars]
    for ch in forbidden_chars:
        if ch and ch in raw_file.stem:
            issues.append(("error",
                f"文件名含禁用字符「{ch}」（逗号会被来源引用按逗号切分书名→断链），改用 ' - '"))
    return [m for s, m in issues if s == "error"]


# ── CLI ─────────────────────────────────────────────────────────

def _stage_0_1_find_project_root() -> Optional[Path]:
    """Walk up from CWD to find a project with a schema.md at its root."""
    cwd = Path.cwd()
    for p in [cwd] + list(cwd.parents):
        if (p / 'schema.md').exists():
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

    project_root = args.project or _stage_0_1_find_project_root()
    if project_root is None:
        print("Error: Could not find schema.md. "
              "Run from a wiki project or use --project.")
        return 1

    raw_root = project_root / 'raw'
    naming_md = project_root / 'schema.md'

    if not naming_md.exists():
        print(f"Error: {naming_md} not found. Create it first.")
        return 1

    rules = _stage_0_1_parse_yaml_block(naming_md.read_text(encoding='utf-8'))
    if not rules:
        print(f"Error: No ```yaml rules``` block found in {naming_md}")
        return 1

    # Load vendor list/prefixes from raw/Datasheet/VENDORS.yaml (datasheet-folder
    # scoped; kept out of schema.md to avoid bloating LLM context). Merged into
    # rules so vendor_field validation in _stage_0_1_check_rule works.
    vendors_file = raw_root / 'Datasheet' / 'VENDORS.yaml'
    if vendors_file.exists():
        vdata = _stage_0_1_parse_simple_yaml(vendors_file.read_text(encoding='utf-8'))
        if vdata.get('vendors'):
            rules['vendors'] = vdata['vendors']
        if vdata.get('vendor_prefixes'):
            rules['vendor_prefixes'] = vdata['vendor_prefixes']

    mode = "🔧 修正" if args.fix else "🔍 检查"
    scope = f"（最近 {args.recent} 分钟内修改的文件）" if args.recent else ""
    print(f"{mode}模式{scope} — {project_root.name}\n")

    results = stage_0_1_scan_raw(raw_root, rules, check=True, fix=args.fix,
                       verbose=args.verbose, recent_minutes=args.recent)

    print(f"\n── 结果 ──")
    print(f"  ✅ 符合规范: {results['ok']}")
    print(f"  ❌ 不符合:   {results['issues']}")
    print(f"  ⚠️  启发式警告: {results['warns']}")
    if args.fix:
        print(f"  🔧 已修正:   {results['fixed']}")
        print(f"  ⚠️  无法自动修正: {results['unfixable']}")

    return 0 if results['issues'] == 0 or (args.fix and results['unfixable'] == 0) else 1


if __name__ == '__main__':
    sys.exit(main())

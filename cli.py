"""
epubkit CLI entry point.

Currently supports the `rename` subcommand which reduces verbose EPUB filenames.
The rename flow is intentionally two-step (plan -> apply) so that no file is
ever renamed without explicit confirmation:

  # 1. Generate a plan (preview to stdout, save to JSON)
  python -m cli rename /path/to/folder --out plan.json

  # 2. Review plan.json, then apply
  python -m cli rename --apply plan.json
"""

from __future__ import annotations

import argparse
import json
import sys
from pathlib import Path

from epub_renamer import reduce_paths


def _color(s: str, code: str) -> str:
    if not sys.stdout.isatty():
        return s
    return f"\033[{code}m{s}\033[0m"


def cmd_rename_plan(args: argparse.Namespace) -> int:
    paths = [Path(p).expanduser().resolve() for p in args.paths]
    missing = [p for p in paths if not p.exists()]
    if missing:
        for p in missing:
            print(f"error: path not found: {p}", file=sys.stderr)
        return 2

    groups = reduce_paths(paths, recursive=args.recursive)
    total_ok = total_unchanged = total_conflict = 0

    plan_entries = []
    for group in groups:
        if not group.proposals:
            continue
        print(_color(f"\n{group.directory}", "1;36"))
        if group.common_prefix:
            print(f"  common prefix: {_color(repr(group.common_prefix), '2')}")
        if getattr(group, "abbreviation", None):
            phrase, acronym = group.abbreviation
            print(f"  abbreviated:   {_color(repr(phrase), '2')} → {acronym}")
        elif getattr(group, "series_acronym", ""):
            print(f"  series acronym: {group.series_acronym}")
        for prop in group.proposals:
            if prop.status == "ok":
                total_ok += 1
                marker = _color("→", "32")
                print(f"  {marker} {prop.old_path.name}")
                print(f"    {prop.new_name}")
                plan_entries.append({
                    "old": str(prop.old_path),
                    "new_name": prop.new_name,
                    "size": prop.old_path.stat().st_size,
                    "mtime_ns": prop.old_path.stat().st_mtime_ns,
                })
            elif prop.status == "conflict":
                total_conflict += 1
                marker = _color("✗", "31")
                print(f"  {marker} {prop.old_path.name}")
                print(f"    -> {prop.new_name}  [CONFLICT: {prop.reason}]")
            else:  # unchanged
                total_unchanged += 1

    print()
    print(f"summary: {_color(str(total_ok), '32')} to rename, "
          f"{total_unchanged} unchanged, "
          f"{_color(str(total_conflict), '31')} conflicts")

    if args.out and plan_entries:
        plan_path = Path(args.out).expanduser().resolve()
        plan_path.write_text(json.dumps({"version": 1, "entries": plan_entries}, indent=2))
        print(f"\nplan written to {plan_path}")
        print(f"apply with: python -m cli rename --apply {plan_path}")
    elif plan_entries and not args.out:
        print("\n(no --out specified — pass --out plan.json to save the plan)")

    return 0 if total_conflict == 0 else 1


def cmd_rename_apply(args: argparse.Namespace) -> int:
    plan_path = Path(args.apply).expanduser().resolve()
    if not plan_path.exists():
        print(f"error: plan not found: {plan_path}", file=sys.stderr)
        return 2

    plan = json.loads(plan_path.read_text())
    entries = plan.get("entries", [])
    if not entries:
        print("plan is empty")
        return 0

    renamed = skipped = failed = 0
    for entry in entries:
        old = Path(entry["old"])
        new = old.parent / entry["new_name"]

        if not old.exists():
            print(_color(f"skip (missing): {old.name}", "33"))
            skipped += 1
            continue

        stat = old.stat()
        if stat.st_size != entry["size"] or stat.st_mtime_ns != entry["mtime_ns"]:
            print(_color(f"skip (modified since plan): {old.name}", "33"))
            skipped += 1
            continue

        if new.exists():
            print(_color(f"skip (target exists): {new.name}", "33"))
            skipped += 1
            continue

        try:
            old.rename(new)
            print(_color(f"✓ {old.name}", "32"))
            print(f"  -> {new.name}")
            renamed += 1
        except OSError as e:
            print(_color(f"✗ {old.name}: {e}", "31"), file=sys.stderr)
            failed += 1

    print(f"\nsummary: {renamed} renamed, {skipped} skipped, {failed} failed")
    return 0 if failed == 0 else 1


def cmd_rename(args: argparse.Namespace) -> int:
    if args.apply:
        return cmd_rename_apply(args)
    if not args.paths:
        print("error: provide one or more paths, or --apply <plan.json>", file=sys.stderr)
        return 2
    return cmd_rename_plan(args)


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(prog="epubkit", description="EPUB toolkit CLI")
    sub = parser.add_subparsers(dest="command", required=True)

    rename = sub.add_parser(
        "rename",
        help="Reduce verbose EPUB filenames (two-step: plan -> apply)",
    )
    rename.add_argument("paths", nargs="*", help="EPUB files or directories")
    rename.add_argument("-r", "--recursive", action="store_true",
                        help="Recurse into subdirectories")
    rename.add_argument("--out", metavar="PLAN.json",
                        help="Save plan to this JSON file")
    rename.add_argument("--apply", metavar="PLAN.json",
                        help="Apply a previously generated plan")
    rename.set_defaults(func=cmd_rename)

    return parser


def main(argv: list[str] | None = None) -> int:
    parser = build_parser()
    args = parser.parse_args(argv)
    return args.func(args)


if __name__ == "__main__":
    sys.exit(main())

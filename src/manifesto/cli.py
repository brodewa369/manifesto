"""CLI: ``manifesto validate|scan|explain|export|diff``

Exit codes (cron/CI friendly):
    0 = clean / valid
    1 = findings (errors; warnings only under --strict); a gated loosening
        under ``diff --strict`` (unless ``--allow-loosening``)
    2 = usage error or unreadable manifest
"""
from __future__ import annotations

import argparse
import json
import sys
from pathlib import Path

from . import __version__
from .diff import (as_json as diff_json, diff_files, gate_hits,
                   render as diff_render)
from .export import TARGETS, export as export_fn
from .explain import explain as explain_fn
from .scan import CLIENT_CONFIGS, render as scan_render, scan as scan_fn
from .schema import (ManifestError, load_manifest_file, split_findings, validate)


def _print_findings(findings):
    for f in findings:
        arrow = f.keypath or "(root)"
        print(f"  {f.severity.upper(): <7} {arrow}: {f.message}")
        if f.hint:
            print(f"          fix: {f.hint}")


def cmd_validate(args) -> int:
    try:
        manifest = load_manifest_file(args.manifest)
    except ManifestError as exc:
        print(f"error: {exc}", file=sys.stderr)
        return 2
    findings = validate(manifest)
    errors, warnings = split_findings(findings)

    if args.json:
        print(json.dumps(
            {"manifest": str(args.manifest), "valid": not errors,
             "error_count": len(errors), "warning_count": len(warnings),
             "findings": [f._asdict() for f in findings]},
            indent=2, ensure_ascii=False))
    else:
        print(f"== manifesto validate | {args.manifest}")
        if findings:
            _print_findings(findings)
        else:
            print("  no findings")
        print(f"== {len(errors)} error(s), {len(warnings)} warning(s)")

    if errors:
        return 1
    if warnings and args.strict:
        return 1
    return 0


def cmd_scan(args) -> int:
    report = scan_fn(home=args.home, client=args.client,
                     allow_subprocess=not args.home)
    if args.json:
        print(json.dumps(report, indent=2, ensure_ascii=False, default=str))
    else:
        print(scan_render(report))
    has_findings = any(f.get("severity") == "error" for f in report["findings"]) or (
        args.strict and report["findings"])
    # scan surfaces 'no manifest' warnings; treat them as findings for exit 1
    if report["findings"] and (args.strict or
                               any(f["code"] == "no_manifest" for f in report["findings"])):
        return 1
    return 1 if has_findings else 0


def cmd_explain(args) -> int:
    try:
        manifest = load_manifest_file(args.manifest)
    except ManifestError as exc:
        print(f"error: {exc}", file=sys.stderr)
        return 2
    print(explain_fn(manifest))
    return 0


def cmd_export(args) -> int:
    try:
        manifest = load_manifest_file(args.manifest)
    except ManifestError as exc:
        print(f"error: {exc}", file=sys.stderr)
        return 2
    text = export_fn(manifest, args.target)
    if args.output:
        Path(args.output).write_text(text, encoding="utf-8")
        print(f"wrote {args.output}", file=sys.stderr)
    else:
        print(text, end="")
    return 0


def cmd_diff(args) -> int:
    try:
        changes = diff_files(args.old, args.new)
    except ManifestError as exc:
        print(f"error: {exc}", file=sys.stderr)
        return 2
    hits = gate_hits(changes)
    old_label, new_label = str(args.old), str(args.new)

    if args.json:
        print(diff_json(old_label, new_label, changes,
                        allow_loosening=args.allow_loosening))
    else:
        print(diff_render(old_label, new_label, changes))

    if hits and args.strict:
        if args.allow_loosening:
            if not args.json:
                print(f"gate: {len(hits)} loosening(s) accepted via --allow-loosening")
            return 0
        if not args.json:
            print("!! GATE (--strict): gated loosening -> exit 1 "
                  "(re-run with --allow-loosening to accept it)")
        return 1
    return 0


def build_parser() -> argparse.ArgumentParser:
    p = argparse.ArgumentParser(
        prog="manifesto",
        description="Portable AI agent job contracts: a manifest schema, validator, "
                    "discovery scanner, and exporters so any enforcer can consume it.")
    p.add_argument("--version", action="version", version=f"%(prog)s {__version__}")
    sub = p.add_subparsers(dest="command", required=True)

    v = sub.add_parser("validate", help="lint a manifest")
    v.add_argument("manifest", type=Path, help="path to agent.manifest.yaml")
    v.add_argument("--json", action="store_true", help="machine-readable findings")
    v.add_argument("--strict", action="store_true", help="warnings also fail (CI)")
    v.set_defaults(func=cmd_validate)

    s = sub.add_parser("scan", help="read-only discovery of running tool-servers")
    s.add_argument("--client", choices=sorted(CLIENT_CONFIGS), default=None)
    s.add_argument("--home", type=Path, default=None,
                   help="treat this dir as the user's home (fixture-testable)")
    s.add_argument("--json", action="store_true")
    s.add_argument("--strict", action="store_true")
    s.set_defaults(func=cmd_scan)

    e = sub.add_parser("explain", help="plain-language summary + risk badges")
    e.add_argument("manifest", type=Path)
    e.set_defaults(func=cmd_explain)

    x = sub.add_parser("export", help="emit an enforcer dialect")
    x.add_argument("manifest", type=Path)
    x.add_argument("--target", choices=TARGETS, default="agentward")
    x.add_argument("-o", "--output", type=Path, default=None)
    x.set_defaults(func=cmd_export)

    d = sub.add_parser("diff", help="compare two contracts; gate loosening")
    d.add_argument("old", type=Path, help="baseline contract (e.g. main)")
    d.add_argument("new", type=Path, help="candidate contract (e.g. this PR)")
    d.add_argument("--json", action="store_true", help="machine-readable report")
    d.add_argument("--strict", action="store_true",
                   help="exit 1 on a gated loosening (payments/fs/shell or "
                        "previously-denied areas)")
    d.add_argument("--allow-loosening", action="store_true",
                   help="explicitly accept the gated loosening (exit 0)")
    d.set_defaults(func=cmd_diff)

    return p


def main(argv=None) -> int:
    parser = build_parser()
    try:
        args = parser.parse_args(argv)
    except SystemExit as exc:
        # argparse exits 2 on usage errors; re-raise to keep the contract
        return int(exc.code) if exc.code is not None else 2
    return args.func(args)


if __name__ == "__main__":
    sys.exit(main())

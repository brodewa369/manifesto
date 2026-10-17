"""CLI: ``manifesto validate|scan|explain|export|diff|check|test``

Exit codes (cron/CI friendly):
    0 = clean / valid
    1 = findings (errors; warnings only under --strict); a gated loosening
        under ``diff --strict`` (unless ``--allow-loosening``)
    2 = usage error or unreadable manifest
    3 = ``check`` verdict REQUIRES_APPROVAL (proceed only with human sign-off)
"""
from __future__ import annotations

import argparse
import json
import sys
from pathlib import Path

from . import __version__
from .batch import (as_json as batch_json, render_batch, render_explain_batch,
                    validate_batch)
from .check import (EXIT as CHECK_EXIT, REQUIRES_APPROVAL, CheckError,
                    as_json as check_json, check as check_fn,
                    render as check_render)
from .diff import (as_json as diff_json, diff_files, gate_hits,
                   render as diff_render)
from .doctor import as_json as doctor_json, render as doctor_render, \
    test_fleet
from .export import TARGETS, export as export_fn
from .explain import explain as explain_fn
from .scan import CLIENT_CONFIGS, render as scan_render, scan as scan_fn
from .schema import (ManifestError, load_manifest_file, load_verb_registry,
                     split_findings, validate)


def _print_findings(findings):
    for f in findings:
        arrow = f.keypath or "(root)"
        print(f"  {f.severity.upper(): <7} {arrow}: {f.message}")
        if f.hint:
            print(f"          fix: {f.hint}")


def cmd_validate(args) -> int:
    registry = None
    if getattr(args, "registry", None) is not None:
        try:
            registry = load_verb_registry(args.registry)
        except ManifestError as exc:
            print(f"error: {exc}", file=sys.stderr)
            return 2

    # --dir: validate every manifest in a directory + collision check
    if getattr(args, "dir", None) is not None:
        try:
            report = validate_batch(args.dir, registry=registry)
        except ManifestError as exc:
            print(f"error: {exc}", file=sys.stderr)
            return 2
        if args.json:
            print(batch_json(report))
        else:
            print(render_batch(report))
        s = report["summary"]
        if s["errors"]:
            return 1
        if s["warnings"] and args.strict:
            return 1
        return 0

    if args.manifest is None:
        print("error: provide a manifest path or --dir DIR", file=sys.stderr)
        return 2
    try:
        manifest = load_manifest_file(args.manifest)
    except ManifestError as exc:
        print(f"error: {exc}", file=sys.stderr)
        return 2
    findings = validate(manifest, registry=registry)
    errors, warnings = split_findings(findings)

    if args.json:
        payload = {"manifest": str(args.manifest), "valid": not errors,
                   "error_count": len(errors), "warning_count": len(warnings),
                   "findings": [f._asdict() for f in findings]}
        if registry is not None:
            payload["registry"] = registry.describe()
        print(json.dumps(payload, indent=2, ensure_ascii=False))
    else:
        print(f"== manifesto validate | {args.manifest}")
        if registry is not None:
            print(f"   registry: {registry.label or args.registry} "
                  f"(mode: {registry.mode}, unknown_verb: {registry.unknown_verb})")
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
    if getattr(args, "dir", None) is not None:
        try:
            print(render_explain_batch(args.dir))
        except ManifestError as exc:
            print(f"error: {exc}", file=sys.stderr)
            return 2
        return 0
    if args.manifest is None:
        print("error: provide a manifest path or --dir DIR", file=sys.stderr)
        return 2
    try:
        manifest = load_manifest_file(args.manifest)
    except ManifestError as exc:
        print(f"error: {exc}", file=sys.stderr)
        return 2
    print(explain_fn(manifest))
    return 0


def cmd_check(args) -> int:
    """Pre-action gate: decide one proposed action against a manifest."""
    registry = None
    if getattr(args, "registry", None) is not None:
        try:
            registry = load_verb_registry(args.registry)
        except ManifestError as exc:
            print(f"error: {exc}", file=sys.stderr)
            return 2
    try:
        manifest = load_manifest_file(args.manifest)
    except ManifestError as exc:
        print(f"error: {exc}", file=sys.stderr)
        return 2

    try:
        decision = check_fn(manifest, args.action, amount=args.amount,
                            rate=args.rate, target=args.target,
                            as_agent=args.as_agent, registry=registry)
    except CheckError as exc:
        print(f"error: {exc}", file=sys.stderr)
        return 2

    if args.json:
        print(check_json(str(args.manifest), decision))
    else:
        print(check_render(str(args.manifest), decision))
    return CHECK_EXIT[decision.verdict]


def cmd_test(args) -> int:
    """Fleet doctor: phantom/undeclared audit against the scanned home."""
    try:
        manifest = load_manifest_file(args.manifest)
    except ManifestError as exc:
        print(f"error: {exc}", file=sys.stderr)
        return 2
    report = test_fleet(manifest, home=args.home, client=args.client,
                        allow_subprocess=not args.home)
    if args.json:
        print(doctor_json(report))
    else:
        print(doctor_render(report))

    s = report["summary"]
    if s["strict_sensitive"] and args.strict:
        return 1
    if args.strict and s["total"]:
        return 1
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

    v = sub.add_parser("validate", help="lint a manifest (or a --dir of them)")
    v.add_argument("manifest", nargs="?", type=Path, default=None,
                   help="path to agent.manifest.yaml")
    v.add_argument("--dir", metavar="DIR", type=Path, default=None,
                   help="validate every manifest in DIR + cross-file "
                        "agent-name collision check")
    v.add_argument("--json", action="store_true", help="machine-readable findings")
    v.add_argument("--strict", action="store_true", help="warnings also fail (CI)")
    v.add_argument("--registry", metavar="FILE", type=Path, default=None,
                   help="org verb-registry override: adds systems, maps verbs to "
                        "risk classes, and sets the unknown-verb rule")
    v.set_defaults(func=cmd_validate)

    s = sub.add_parser("scan", help="read-only discovery of running tool-servers")
    s.add_argument("--client", choices=sorted(CLIENT_CONFIGS), default=None)
    s.add_argument("--home", type=Path, default=None,
                   help="treat this dir as the user's home (fixture-testable)")
    s.add_argument("--json", action="store_true")
    s.add_argument("--strict", action="store_true")
    s.set_defaults(func=cmd_scan)

    e = sub.add_parser("explain", help="plain-language summary + risk badges")
    e.add_argument("manifest", nargs="?", type=Path, default=None)
    e.add_argument("--dir", metavar="DIR", type=Path, default=None,
                   help="explain every manifest in DIR")
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

    c = sub.add_parser("check", help="pre-action gate: ALLOW/DENY/REQUIRES_APPROVAL "
                                     "for one proposed action")
    c.add_argument("manifest", type=Path, help="path to agent.manifest.yaml")
    c.add_argument("--action", required=True, metavar="SYSTEM.VERB",
                   help="the proposed action, e.g. payments.charge or fs.write")
    c.add_argument("--amount", type=float, default=None,
                   help="proposed money amount (checked against amount.max)")
    c.add_argument("--rate", type=float, default=None,
                   help="proposed call count (checked against rate.max)")
    c.add_argument("--target", default=None,
                   help="proposed outbound target (checked against to_domains)")
    c.add_argument("--as", dest="as_agent", default=None, metavar="SUB-AGENT",
                   help="evaluate as this delegated sub-agent (its tightened slice)")
    c.add_argument("--registry", metavar="FILE", type=Path, default=None,
                   help="org verb-registry whose gated risk classes force approval")
    c.add_argument("--json", action="store_true", help="machine-readable decision")
    c.set_defaults(func=cmd_check)

    t = sub.add_parser("test", help="fleet doctor: phantom/undeclared audit "
                                    "against the scanned home")
    t.add_argument("manifest", type=Path, help="path to agent.manifest.yaml")
    t.add_argument("--home", type=Path, default=None,
                   help="treat this dir as the user's home (fixture-testable)")
    t.add_argument("--client", choices=sorted(CLIENT_CONFIGS), default=None,
                   help="restrict discovery to one client")
    t.add_argument("--json", action="store_true", help="machine-readable report")
    t.add_argument("--strict", action="store_true",
                   help="exit 1 on any finding; an UNDECLARED sensitive tool always "
                        "fails under --strict")
    t.set_defaults(func=cmd_test)

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

"""Tiny JSON Schema (draft-07 subset) engine — zero dependencies.

This is the *conformity engine*: it walks a **published** schema document
(``schema/agent-manifest.schema.json``) at call time and turns structural
violations into findings. It is deliberately **not** a full ``jsonschema``
implementation — it supports exactly the draft-07 keywords the published
schema uses:

===========================  ==================================================
Keyword                      Meaning
===========================  ==================================================
``type``                     string or list of strings
``required``                 list of mandatory property names
``properties``               map of property name -> subschema
``additionalProperties``     ``false`` (reject unknowns) or a subschema
``items``                    subschema applied to every array element
``enum``                     allowed scalar values
``pattern``                  regex a string value must match
``minLength``                minimum string length
``oneOf``                    exactly one subschema must match
``propertyNames``            subschema every property *name* must satisfy
``$ref``                     local ``#/...`` JSON pointer
===========================  ==================================================

``$schema``/``$id``/``title``/``description`` are annotation-only (ignored).

Two optional, **non-normative** annotations let the *schema itself* name the
finding code its violations should produce. Real draft-07 validators ignore
unknown keywords, so the document stays spec-valid:

=======================  ==================================================
``x-violation-code``     finding code when a value violates THIS subschema
``x-required-code``      finding code when a required property is missing
=======================  ==================================================

Because the engine reads the schema document on every ``validate()`` call,
mutating the schema file (e.g. adding ``ttl`` to the root ``required`` list)
changes validator behaviour with **no code change** — proven by
``tests/test_schema_canary.py``.
"""
from __future__ import annotations

import re
from collections import namedtuple

#: Structural finding emitted by the engine (mirrors schema.Finding fields).
StructuralFinding = namedtuple(
    "StructuralFinding", ["keypath", "code", "severity", "message", "hint"])

ERROR = "error"


def resolve_ref(root, ref):
    """Resolve a local ``#/...`` JSON pointer against the schema root."""
    if not isinstance(ref, str) or not ref.startswith("#/"):
        raise ValueError(f"only local '#/...' refs are supported, got {ref!r}")
    node = root
    for part in ref[2:].split("/"):
        part = part.replace("~1", "/").replace("~0", "~")
        node = node[part]
    return node


def _type_ok(value, t) -> bool:
    if t == "object":
        return isinstance(value, dict)
    if t == "array":
        return isinstance(value, list)
    if t == "string":
        return isinstance(value, str)
    if t == "boolean":
        return isinstance(value, bool)
    if t == "null":
        return value is None
    if t == "integer":
        return isinstance(value, int) and not isinstance(value, bool)
    if t == "number":
        return isinstance(value, (int, float)) and not isinstance(value, bool)
    return True  # unknown type keyword -> do not fail closed on it


def _type_name(value) -> str:
    if isinstance(value, bool):
        return "boolean"
    if isinstance(value, dict):
        return "object"
    if isinstance(value, list):
        return "array"
    if isinstance(value, str):
        return "string"
    if value is None:
        return "null"
    if isinstance(value, int):
        return "integer"
    if isinstance(value, float):
        return "number"
    return type(value).__name__


class Engine:
    """Walk ``data`` against ``schema`` (a draft-07 subset document)."""

    def __init__(self, schema):
        if not isinstance(schema, dict):
            raise ValueError("schema root must be an object")
        self.schema = schema

    def validate(self, data):
        findings = []
        self._walk(data, self.schema, "", findings, self.schema)
        return findings

    # -- helpers --------------------------------------------------------- #
    @staticmethod
    def _code(subschema, default):
        return subschema.get("x-violation-code", default)

    @staticmethod
    def _kp(keypath, seg):
        return f"{keypath}.{seg}" if keypath else str(seg)

    def _emit(self, findings, keypath, code, message, hint=""):
        findings.append(StructuralFinding(keypath, code, ERROR, message, hint))

    # -- the walk -------------------------------------------------------- #
    def _walk(self, value, subschema, keypath, findings, root):
        if not isinstance(subschema, dict):
            return

        if "$ref" in subschema:
            self._walk(value, resolve_ref(root, subschema["$ref"]),
                       keypath, findings, root)
            return

        label = keypath or "(root)"

        # type ---------------------------------------------------------- #
        if "type" in subschema:
            types = subschema["type"]
            if isinstance(types, str):
                types = [types]
            if not any(_type_ok(value, t) for t in types):
                self._emit(findings, keypath, self._code(subschema, "bad_type"),
                           f"{label} must be {' or '.join(types)}, "
                           f"got {_type_name(value)}")
                return  # do not descend past a type mismatch

        # enum ---------------------------------------------------------- #
        if "enum" in subschema and value not in subschema["enum"]:
            self._emit(findings, keypath, self._code(subschema, "bad_enum"),
                       f"{label} must be one of {subschema['enum']}, got {value!r}")
            return

        # pattern (string values) ---------------------------------------- #
        if "pattern" in subschema and isinstance(value, str):
            if not re.search(subschema["pattern"], value):
                self._emit(findings, keypath, self._code(subschema, "bad_pattern"),
                           f"{label} does not match the required pattern")
                return

        # minLength ------------------------------------------------------- #
        if ("minLength" in subschema and isinstance(value, str)
                and len(value) < subschema["minLength"]):
            self._emit(findings, keypath, self._code(subschema, "bad_length"),
                       f"{label} must not be empty", "provide a non-empty value")
            return

        # oneOf ----------------------------------------------------------- #
        if "oneOf" in subschema:
            matched = 0
            for sub in subschema["oneOf"]:
                sub_findings = []
                self._walk(value, sub, keypath, sub_findings, root)
                if not sub_findings:
                    matched += 1
            if matched != 1:
                self._emit(findings, keypath, self._code(subschema, "bad_type"),
                           f"{label} does not match exactly one allowed shape "
                           f"({len(subschema['oneOf'])} candidates, {matched} matched)")
                return

        # object: propertyNames / required / properties / additionalProps -- #
        if isinstance(value, dict):
            props = subschema.get("properties", {})

            pn = subschema.get("propertyNames")
            if isinstance(pn, dict):
                pat = pn.get("pattern")
                for name in value:
                    if isinstance(name, str) and pat and not re.search(pat, name):
                        self._emit(findings, self._kp(keypath, name),
                                   pn.get("x-violation-code", "bad_pattern"),
                                   f"'{name}' is not a valid key")

            for req in subschema.get("required", []):
                if req not in value:
                    req_sub = props.get(req)
                    code = (req_sub.get("x-required-code", "missing_required")
                            if isinstance(req_sub, dict) else "missing_required")
                    self._emit(findings, self._kp(keypath, req), code,
                               f"required field '{req}' is missing")

            ap = subschema.get("additionalProperties", True)
            for name, item in value.items():
                if name in props:
                    self._walk(item, props[name], self._kp(keypath, name),
                               findings, root)
                elif ap is False:
                    self._emit(findings, self._kp(keypath, name), "unknown_key",
                               f"unknown key '{name}'",
                               f"allowed: {', '.join(sorted(props))}")
                elif isinstance(ap, dict):
                    self._walk(item, ap, self._kp(keypath, name), findings, root)

        # array: items ---------------------------------------------------- #
        if isinstance(value, list) and "items" in subschema:
            for i, item in enumerate(value):
                self._walk(item, subschema["items"], f"{keypath}[{i}]",
                           findings, root)

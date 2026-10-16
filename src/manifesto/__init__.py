"""manifesto — a portable, human-readable "AI agent job contract".

The agent-governance space has plenty of *enforcers* (AgentWard, AgentBound,
Permit.io, ...), each speaking its own incompatible policy dialect. What has
been missing is a **portable, human-readable, diff-able spec** that sits above
all of them: a manifest schema, a validator/linter, a discovery scanner, and
exporters so *any* enforcer can consume it.

manifesto is the unowned glue layer. It is explicitly **not** a runtime proxy —
it never sits in the request path. It reads a declarative ``agent.manifest.yaml``
contract, tells you whether it is sound, discovers undeclared agents already
running on a machine, and exports an approximate policy for a target enforcer.

Zero runtime dependencies — pure Python standard library.
"""

__version__ = "0.1.0"

"""Governance package — policy-layer guardrails on top of the permission engine.

The :class:`~coworker.permissions.PermissionEngine` decides allow / deny / ask
per call. This package adds a second, independent signal: a regex-signature
**Tool Guard** that flags dangerous *shell commands* (destructive, evasive,
privilege-escalating) before they run. Findings feed back into ``evaluate()``:

  * CRITICAL finding  -> the call is DENIED outright (catastrophic).
  * HIGH / MEDIUM     -> the call must be approved by the user.
  * ``execution_level="strict"`` promotes ANY finding to an approval prompt.

Kernel-level OS/secret confinement is the separate :mod:`coworker.sandbox`
layer; the two are complementary.
"""

from __future__ import annotations

from .tool_guard import Finding, Rule, RuleGuardian

__all__ = ["Finding", "Rule", "RuleGuardian"]

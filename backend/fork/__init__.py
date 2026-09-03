"""Fork-owned backend code.

Everything this fork adds to the backend lives here rather than inside upstream
packages, so upstream files stay byte-identical and upstream package guardrails
(source-file thresholds, AGENTS budgets) are never tipped over by a fork file.

See dev/unified-main/00-upstream-touch-policy.md.
"""

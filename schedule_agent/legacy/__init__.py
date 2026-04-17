"""Deprecated compatibility layer.

Keep all support for pre-migration queue state and job schemas in this package.
New features should not depend on anything here.
"""

DEPRECATION_NOTE = (
    "Deprecated compatibility surface. Keep legacy imports isolated under "
    "`schedule_agent.legacy` and remove them after the migration window closes."
)

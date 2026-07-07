"""CLI deploy compatibility exports.

The executable still routes through ``agentify.py`` today, but keeping this
module wired prevents the deploy surface from drifting as the package grows.
"""

from invoke.deploy import (
    DeployPlan,
    DeployResult,
    build_deploy_plan,
    deploy_claude_agent,
    write_local_deploy_record,
)

__all__ = [
    "DeployPlan",
    "DeployResult",
    "build_deploy_plan",
    "deploy_claude_agent",
    "write_local_deploy_record",
]

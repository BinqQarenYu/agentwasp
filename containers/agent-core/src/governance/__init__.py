"""Resource Governance Layer — prevents runaway task/goal/agent creation."""
from .governor import ResourceGovernor

__all__ = ["ResourceGovernor"]

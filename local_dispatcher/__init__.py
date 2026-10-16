"""Local Coding Agent Dispatcher.

Small, deterministic orchestration service: the primary Claude Code session
stays the lead implementer; this service supervises bounded worker jobs on
secondary backends. Capacity, isolation, durability and recovery are enforced
here, not in prompts.
"""

__version__ = "0.1.0"

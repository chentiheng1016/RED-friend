"""Long-running daemon process implementations (telegram bot, etc.).

Modules in this package own the lifecycle of a single launchd-managed
process. The previous flat layout in agent_core/daemon_*.py is being
split here one subpackage at a time, with shim re-exports left at the
old paths so launchd plists and tests don't need to change in lockstep.
"""

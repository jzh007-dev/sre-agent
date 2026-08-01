# Deliberately empty.
#
# No re-exports: `from agent.tools import X` executes this file and pulls in
# whatever it imports. If it re-exported `stubs.default_tool_registry`, then
# `core/loop.py` importing `agent.tools.protocol` would transitively import a
# concrete tool implementation, and the seam-rule test in
# tests/test_architecture.py would be measuring nothing.
#
# Import the submodule you need: agent.tools.protocol, agent.tools.dispatch,
# agent.tools.stubs.

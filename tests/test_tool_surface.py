"""tool_surface is the governed tool registry that lower layers use instead of server.

server.py registers every tool into tool_surface and installs its policy guard.
Workflows, the LangGraph dispatcher, the chat worker, the skill catalog and the
capability inventory look tools up there, so they never import the MCP adapter.
"""

import subprocess
import sys

from unified_agent import server, tool_surface


def test_server_registry_is_the_tool_surface_registry():
    assert server.REGISTERED_TOOLS is tool_surface.REGISTERED_TOOLS
    assert tool_surface._guard is server._guarded_and_instrumented
    assert "task_add" in tool_surface.registry()


def test_guard_wraps_like_registration():
    from unified_agent.tools import tasks

    wrapped = tool_surface.guard(tasks.task_list)
    assert wrapped.__name__ == "task_list"
    assert wrapped is not tasks.task_list


def test_registry_loads_the_hub_surface_on_first_use():
    code = (
        "import sys\n"
        "from unified_agent import tool_surface\n"
        "assert 'unified_agent.server' not in sys.modules\n"
        "names = tool_surface.registry()\n"
        "assert 'unified_agent.server' in sys.modules\n"
        "assert 'workflow_run' in names and 'task_add' in names\n"
    )
    result = subprocess.run([sys.executable, "-c", code], capture_output=True, text=True)
    assert result.returncode == 0, result.stderr

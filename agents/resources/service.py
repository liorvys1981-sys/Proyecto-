import resource
import sys
import time
from typing import Any, Awaitable, Callable

from agents.contracts import AgentDecision


def measure(function):
    cpu_before = time.thread_time()
    result = function()
    cpu_milliseconds = max(1, round((time.thread_time() - cpu_before) * 1000))
    peak_memory = resource.getrusage(resource.RUSAGE_SELF).ru_maxrss
    memory_bytes = int(peak_memory if peak_memory > 10**8 else peak_memory * 1024)
    return result, cpu_milliseconds, memory_bytes


async def measure_async(
    function: Callable[..., Awaitable[Any]], *args: Any, **kwargs: Any
) -> tuple[Any, int, int]:
    if sys.platform == "win32":
        raise RuntimeError("Resource measurements require a supported Unix runtime")
    cpu_before = time.thread_time()
    result = await function(*args, **kwargs)
    cpu_milliseconds = max(0, round((time.thread_time() - cpu_before) * 1000))
    peak_memory = resource.getrusage(resource.RUSAGE_SELF).ru_maxrss
    memory_bytes = int(peak_memory if sys.platform == "darwin" else peak_memory * 1024)
    return result, cpu_milliseconds, memory_bytes


def resource_decision(cpu_milliseconds: int, peak_memory_bytes: int) -> AgentDecision:
    return AgentDecision(
        agent="resources",
        status="measured",
        evidence={
            "cpu_milliseconds": cpu_milliseconds,
            "process_peak_memory_bytes": peak_memory_bytes,
            "memory_scope": "process_peak_not_per_task_allocation",
        },
    )

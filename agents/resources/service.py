import resource
import time


def measure(function):
    cpu_before = time.thread_time()
    result = function()
    cpu_milliseconds = max(1, round((time.thread_time() - cpu_before) * 1000))
    peak_memory = resource.getrusage(resource.RUSAGE_SELF).ru_maxrss
    memory_bytes = int(peak_memory if peak_memory > 10**8 else peak_memory * 1024)
    return result, cpu_milliseconds, memory_bytes

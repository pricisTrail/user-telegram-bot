"""Register async callbacks without importing their optional integrations."""

from importlib import import_module
from inspect import isawaitable


def lazy_async_callback(module, name, *, package=None):
    target = None

    async def dispatch(*args, **kwargs):
        nonlocal target
        if target is None:
            candidate = getattr(import_module(module, package), name)
            if not callable(candidate):
                raise TypeError("The command callback is not callable")
            target = candidate
        result = target(*args, **kwargs)
        # Await the callback once. A @new_task callback returns its scheduled
        # Task from that await; preserve that behavior instead of awaiting it too.
        return await result if isawaitable(result) else result

    dispatch.__name__ = name
    dispatch.__qualname__ = name
    dispatch.__doc__ = f"Load {name} when its command is first used."
    return dispatch

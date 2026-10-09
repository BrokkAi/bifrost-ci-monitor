"""An optional command deadline, scoped to one supervisor invocation."""
from contextvars import ContextVar
import time


class Deferred(Exception):
    """A bounded operation is unfinished; its durable action remains pending."""


deadline = ContextVar('supervisor_command_deadline', default=None)


def timeout(requested):
    end = deadline.get()
    if end is None:
        return requested
    remaining = end - time.monotonic()
    if remaining <= 0:
        raise Deferred('supervisor command budget exhausted')
    return min(requested, remaining)


def expired():
    end = deadline.get()
    return end is not None and time.monotonic() >= end - .1

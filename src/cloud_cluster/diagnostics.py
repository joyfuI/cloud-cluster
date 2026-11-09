"""Fields safe to put in the application's rotating log."""

import logging
import os
from hashlib import blake2s

from .domain import BackendError, ErrorKind

logger = logging.getLogger("cloud_cluster")
_reference_key = os.urandom(16)


def diagnostic_ref(value):
    return blake2s(
        value.encode("utf-8", errors="surrogatepass"), key=_reference_key, digest_size=8
    ).hexdigest()


def error_summary(error, traceback=None):
    name = type(error).__name__
    if isinstance(error, BackendError) and isinstance(error.kind, ErrorKind):
        name += f"/{error.kind.name}"
    elif isinstance(error, OSError) and type(error.errno) is int:
        name += f"/errno={error.errno}"
        if type(getattr(error, "winerror", None)) is int:
            name += f"/winerror={error.winerror}"
    if isinstance(error, BackendError) and error.diagnostic:
        name += f" {error.diagnostic}"
    frame = traceback or error.__traceback__
    if frame is None:
        return name
    while frame.tb_next is not None:
        frame = frame.tb_next
    module = frame.tb_frame.f_globals.get("__name__", "?")
    return f"{name} at {module}.{frame.tb_frame.f_code.co_name}:{frame.tb_lineno}"

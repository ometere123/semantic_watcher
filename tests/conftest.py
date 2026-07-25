"""Test configuration.

Contains one Windows-only shim. Nothing here affects contract behaviour.
"""

import atexit
import os
import sys

import pytest


@pytest.fixture(autouse=True)
def _reset_contract_registry():
    """Allow each test to load whichever contract it needs.

    The SDK permits a single ``gl.Contract`` subclass per process and records
    it in a module-level global. Without clearing that between tests, a suite
    covering more than one contract passes or fails purely on file ordering:
    whichever contract loads first wins and every later deploy raises
    "only one contract is allowed".
    """
    yield
    try:
        import genlayer.gl.genvm_contracts as contracts
    except ImportError:
        return
    contracts.__known_contract__ = None


CONTRACTS_DIR = os.path.join(os.path.dirname(os.path.dirname(__file__)), "contracts")
REFERENCE_CONTRACT = os.path.join(CONTRACTS_DIR, "semantic_watcher.py")


def as_address(value):
    """Coerce a direct-mode account fixture into a genlayer ``Address``.

    The account fixtures fall back to raw bytes when the SDK is not importable
    at fixture-resolution time, which is the normal case: gltest only extracts
    and puts the SDK on ``sys.path`` during a deploy. Passing those bytes
    straight into a parameter annotated ``Address`` then fails inside storage.

    Rather than requiring callers to deploy something first, we put the SDK on
    the path ourselves when the import is not yet available.
    """
    if not isinstance(value, bytes):
        return value

    try:
        from genlayer.py.types import Address
    except ImportError:
        from pathlib import Path

        from gltest.direct.sdk_loader import setup_sdk_paths

        setup_sdk_paths(Path(REFERENCE_CONTRACT), None)
        from genlayer.py.types import Address

    return Address(value)

# ---------------------------------------------------------------------------
# Windows shim for gltest direct mode
#
# gltest's direct-mode loader injects the transaction message by writing it to
# a temp file, dup2-ing that file onto fd 0, and then immediately unlinking the
# path. On POSIX the unlink succeeds because the descriptor keeps the inode
# alive. Windows refuses to unlink a file that still has an open handle, so
# every direct-mode deploy raises PermissionError (WinError 32).
#
# We let the unlink fail quietly and sweep the leaked temp files at exit. This
# is a host-platform workaround, not a contract concern -- on Linux and macOS
# this block is skipped entirely and gltest runs unmodified.
# ---------------------------------------------------------------------------

if sys.platform == "win32":  # pragma: no cover - platform specific
    try:
        from gltest.direct import loader as _gltest_loader
    except ImportError:  # gltest not installed; nothing to patch
        _gltest_loader = None

    if _gltest_loader is not None:
        _leaked_paths: list[str] = []
        _real_unlink = os.unlink

        def _tolerant_unlink(path, *args, **kwargs):
            try:
                return _real_unlink(path, *args, **kwargs)
            except PermissionError:
                _leaked_paths.append(os.fspath(path))

        _original_inject = _gltest_loader._inject_message_to_fd0

        def _inject_message_to_fd0(vm):
            """Run the original injection with a Windows-tolerant unlink."""
            os.unlink = _tolerant_unlink
            try:
                return _original_inject(vm)
            finally:
                os.unlink = _real_unlink

        _gltest_loader._inject_message_to_fd0 = _inject_message_to_fd0

        @atexit.register
        def _sweep_leaked_temp_files():
            for path in _leaked_paths:
                try:
                    _real_unlink(path)
                except OSError:
                    pass

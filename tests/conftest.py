import sys
from pathlib import Path

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "src"))

from ccbus.store import Bus  # noqa: E402


@pytest.fixture
def root(tmp_path, monkeypatch):
    """A scratch bus root, also exported as CCBUS_DIR for CLI-level tests."""
    bus_dir = tmp_path / "bus"
    monkeypatch.setenv("CCBUS_DIR", str(bus_dir))
    return bus_dir


@pytest.fixture
def bus(root):
    b = Bus(root)
    yield b
    b.close()


@pytest.fixture
def run(root, capsys):
    """Invoke the CLI in-process; returns (exit_code, stdout, stderr)."""
    from ccbus.cli import main

    def _run(*argv, stdin=None, monkey=None):
        import io
        import sys as _sys
        if stdin is not None:
            old = _sys.stdin
            _sys.stdin = io.TextIOWrapper(io.BytesIO(stdin), encoding="utf-8")
            try:
                code = main(list(argv))
            finally:
                _sys.stdin = old
        else:
            code = main(list(argv))
        out = capsys.readouterr()
        return code, out.out, out.err

    return _run

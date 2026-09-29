"""Run the real JavaScript bridge regression suite if Node is available."""
import shutil
import subprocess
from pathlib import Path
import pytest


@pytest.mark.skipif(shutil.which("node") is None, reason="Node is required for bridge JavaScript tests")
def test_native_bridge_async_protocol():
    script = Path(__file__).with_name("bridge_reliability.test.cjs")
    result = subprocess.run([shutil.which("node"), "--test", str(script)],
                            capture_output=True, text=True, encoding="utf-8", timeout=30)
    assert result.returncode == 0, result.stdout + result.stderr

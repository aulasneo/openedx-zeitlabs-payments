"""Exercise simultaneous initiation against committed data on separate connections."""

import subprocess
import sys
from pathlib import Path


def test_simultaneous_requests_claim_payment_once():
    """Isolate the file database so transaction-test cleanup cannot flush shared fixtures."""
    result = subprocess.run(
        [sys.executable, '-m', 'test_utils.initiation_concurrency'],
        cwd=Path(__file__).resolve().parents[1],
        capture_output=True,
        text=True,
        timeout=60,
        check=False,
    )
    assert result.returncode == 0, result.stdout + result.stderr

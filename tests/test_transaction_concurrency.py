"""Verify legacy migration safety and real races without flushing shared fixtures."""

import os
import subprocess
import sys
from pathlib import Path

import pytest


@pytest.mark.parametrize('mode', ['legacy', 'sqlite', 'mysql'])
def test_payment_database_safety(mode):
    """CI supplies MySQL for full row-lock/fulfillment races; SQLite tests always run."""
    if mode == 'mysql' and os.environ.get('PAYMENT_TEST_MYSQL') != '1':
        pytest.skip('Set PAYMENT_TEST_MYSQL=1 to run the InnoDB concurrency integration test')
    result = subprocess.run(
        [sys.executable, '-m', 'test_utils.transaction_races', mode],
        cwd=Path(__file__).resolve().parents[1], capture_output=True, text=True, timeout=120, check=False,
    )
    assert result.returncode == 0, result.stdout + result.stderr

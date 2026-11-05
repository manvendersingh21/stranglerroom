"""Stand-in long-running agent used by tests/test_ops_scripts.py."""
import signal
import sys
import time

signal.signal(signal.SIGTERM, lambda *_: sys.exit(0))
while True:
    time.sleep(0.5)

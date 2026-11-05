"""Stand-in agent that ignores SIGTERM (exercises agents.sh stop's KILL escalation)."""
import signal
import time

signal.signal(signal.SIGTERM, signal.SIG_IGN)
while True:
    time.sleep(0.2)

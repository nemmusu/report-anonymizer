"""Cross-platform fake llama-server used by test_server_lifecycle.py.

Prints the two lines ``ServerManager`` looks for in stdout to consider the
server "ready", optionally spawns N grandchildren (so descendant-cleanup
tests have something for ``terminate_process_tree`` to walk), then sleeps
forever until killed.

Honours both ``KeyboardInterrupt`` (POSIX SIGINT, Windows Ctrl+C) and
``SystemExit`` so the process can be torn down cleanly via either:

  * POSIX: ``os.killpg(pgid, SIGTERM)`` -> SIGTERM -> KeyboardInterrupt.
  * Windows: ``GenerateConsoleCtrlEvent(CTRL_BREAK_EVENT, pgid)`` ->
    KeyboardInterrupt in the Python interpreter.

Unknown CLI flags are silently ignored: the real ``server_manager`` passes
``--model``, ``--host``, ``--port``, ``-c``, ``-ngl`` etc. which would
otherwise blow up argparse.
"""
from __future__ import annotations

import argparse
import subprocess
import sys
import time


def main() -> None:
    parser = argparse.ArgumentParser(add_help=False)
    parser.add_argument("--children", type=int, default=1)
    args, _unknown = parser.parse_known_args()

    # These exact phrases are what ``ServerManager._wait_until_ready`` (or
    # equivalent) scans stdout for. Print and flush immediately so the test
    # doesn't time out before the writes hit the parent's pipe.
    print("model loaded", flush=True)
    print("HTTP server listening", flush=True)

    # Spawn grandchildren so terminate_process_tree has something to walk.
    # Each grandchild runs the same script with --children 0 so we don't
    # fork-bomb.
    for _ in range(max(0, args.children)):
        subprocess.Popen(
            [sys.executable, __file__, "--children", "0"],
            close_fds=True,
        )

    try:
        while True:
            time.sleep(60)
    except (KeyboardInterrupt, SystemExit):
        sys.exit(0)


if __name__ == "__main__":
    main()

"""Supervised Redis consumers. A failed child fails the container, never a silent thread."""

from __future__ import annotations

import argparse
import logging
import os
import signal
import subprocess
import sys
import time

from .bootstrap import Role, bootstrap
from .queue_config import QUEUES


def run(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--queue', choices=[queue.name for queue in QUEUES])
    parser.add_argument('--check', action='store_true', help='validate admission and Redis connectivity, then exit')
    args = parser.parse_args(argv)
    logging.basicConfig(level=logging.INFO)
    bootstrap(Role.WORKER)
    selected = [queue for queue in QUEUES if args.queue is None or queue.name == args.queue]
    for queue in selected:
        queue.validate()

    from utils import cloud_tasks_redis

    cloud_tasks_redis._r().ping()
    if args.check:
        return 0
    if args.queue is not None:
        os.environ['QUEUE_REDIS_WORKER_SECRET'] = os.environ[selected[0].secret_env]
        cloud_tasks_redis._worker(args.queue)
        # A queue consumer has no successful finite completion.
        return 1

    children: list[subprocess.Popen] = []
    stopping = False

    def stop(_signum, _frame):
        nonlocal stopping
        stopping = True

    previous = {sig: signal.signal(sig, stop) for sig in (signal.SIGTERM, signal.SIGINT)}
    try:
        for queue in selected:
            children.append(subprocess.Popen([sys.executable, '-m', 'fork.worker', '--queue', queue.name]))
        while not stopping:
            if any(child.poll() is not None for child in children):
                return 1
            time.sleep(0.2)
        return 0
    finally:
        for child in children:
            if child.poll() is None:
                child.terminate()
        for child in children:
            try:
                child.wait(timeout=5)
            except subprocess.TimeoutExpired:
                child.kill()
                child.wait()
        for sig, handler in previous.items():
            signal.signal(sig, handler)


if __name__ == '__main__':
    raise SystemExit(run())

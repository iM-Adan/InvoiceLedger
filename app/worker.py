"""Vercel daemon that processes queued invoice runs in order."""
from __future__ import annotations

import time

from . import db, pipeline


def main() -> None:
    db.conn()
    db.mark_interrupted_runs("Interrupted by worker restart - please re-run")
    while True:
        run_id = db.claim_next_run()
        if run_id is None:
            time.sleep(1)
            continue
        pipeline.process(run_id)


if __name__ == "__main__":
    main()

"""
Web platform, Section 4 — Celery application object.

Decouples Stockfish analysis (CPU-bound, seconds to low minutes per game)
from the HTTP request that triggers it. Redis backs both the task queue
(broker) and result storage (backend) — one moving part instead of two for
a project this size; a larger deployment could split them, but there's no
reason to here.

Start a worker (from the `app/` directory, so it can import db/config/etc.
the same way server.py does):

    celery -A celery_app worker --loglevel=info

Windows note: Celery's default "prefork" pool needs os.fork(), which
Windows doesn't have. Use the solo pool (one task at a time) or the
threads pool (--pool=threads --concurrency=4) instead:

    celery -A celery_app worker --loglevel=info --pool=solo
"""

from celery import Celery

from config import REDIS_URL

celery_app = Celery(
    "chess_tracker",
    broker=REDIS_URL,
    backend=REDIS_URL,
    include=["tasks"],
)

celery_app.conf.update(
    task_serializer="json",
    result_serializer="json",
    accept_content=["json"],
    result_expires=60 * 60 * 24,  # a day is plenty for a status-polling UI
    task_track_started=True,
    # Each task can take a while (Stockfish analysis) — a worker greedily
    # prefetching several would sit on games it hasn't started while other
    # workers idle, so keep prefetch to exactly what's being worked on.
    worker_prefetch_multiplier=1,
    # A worker killed mid-analysis (deploy, OOM, crash) re-queues the game
    # instead of silently losing it — the tradeoff is a task can run twice
    # if the worker dies AFTER finishing but BEFORE acking, which is fine
    # here since analyze_and_store_game()'s own DB writes aren't harmed by
    # being repeated.
    task_acks_late=True,
    task_reject_on_worker_lost=True,
    # Bounds a HUNG (not crashed) worker — a Stockfish subprocess that
    # deadlocks or gets stuck on a corrupted position leaves the worker
    # process itself alive, so task_acks_late/task_reject_on_worker_lost
    # above (which only help once a worker actually dies) never trigger,
    # and games.analysis_status stays "processing" forever with nothing to
    # ever notice or report it. Deliberately generous — normal analysis is
    # "under a minute" per config.py's own STOCKFISH_DEPTH comment, even a
    # long game at a high custom `depth` (this endpoint's own query param)
    # shouldn't remotely approach this; it's a backstop for a genuinely
    # stuck process, not a performance budget. The soft limit raises
    # SoftTimeLimitExceeded inside the task, caught by
    # analyze_game_task's own except Exception (same retry-or-fail path as
    # any other analysis error); the hard limit past that is a last-resort
    # kill if even the soft-limit handling itself hangs.
    task_soft_time_limit=600,
    task_time_limit=660,
)

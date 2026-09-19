"""
A persistent pool of Stockfish workers for analyzing many independent
positions in parallel — what the game review (a search for every position of
a game, both colors) and the bulk "review all my games" job need.

Each worker process starts its own single-threaded Stockfish lazily on its
first task and keeps it for the life of the pool, so analyzing a game after
the first pays no per-task process-startup cost. Independent positions
parallelize almost perfectly, which is why this is a set of single-threaded
engines in separate processes rather than one multi-threaded engine.

Not a concurrent.futures.ProcessPoolExecutor on purpose: python-chess runs
each engine on a NON-daemon background thread, so a pool worker holding a
live engine can never exit on its own, and the executor's own shutdown then
waits on it forever (the process hangs at exit). These workers are daemon
processes that quit their engine when told to, and also notice and exit if
the parent process dies.

Safe to start from a web server's background thread: the worker function
lives in this real module, not in `__main__`, so a spawned child never needs
to re-execute the server to find it (verified on Windows under `-m uvicorn`,
see CHANGELOG v43). Scripts that call this must themselves sit behind an
`if __name__ == "__main__":` guard, as on any Windows multiprocessing use.
"""

from __future__ import annotations

import atexit
import logging
import multiprocessing
import os
import queue
import threading
import time

import chess
import chess.engine

from config import STOCKFISH_MAX_SECONDS_PER_POSITION

logger = logging.getLogger(__name__)

# How many plies of each engine line to keep (what "Best was..." and the
# explanations replay).
MAX_PV_PLIES = 10

# Per-process (worker side only): the long-lived engine.
_engine: chess.engine.SimpleEngine | None = None


def default_workers() -> int:
    """Workers for bulk review: most of the machine, but leave headroom for
    the web server and whatever else the person is doing."""
    override = os.environ.get("REVIEW_WORKERS")
    if override:
        return max(1, int(override))
    return max(1, min(8, (os.cpu_count() or 4) - 2))


def _get_engine() -> chess.engine.SimpleEngine:
    global _engine
    if _engine is None:
        from analysis import get_engine  # imported here: keeps module import light
        _engine = get_engine()
    return _engine


def _quit_engine() -> None:
    global _engine
    if _engine is not None:
        try:
            _engine.quit()
        except Exception:
            pass
        _engine = None


def _analyse(fen: str, depth: int, multipv: int) -> list[dict]:
    """Top `multipv` lines at `fen`, mover's perspective:
    [{"uci", "cp" (None if mate), "mate" (None if not), "pv": [uci, ...]}]."""
    board = chess.Board(fen)
    limit = chess.engine.Limit(depth=depth, time=STOCKFISH_MAX_SECONDS_PER_POSITION)
    try:
        infos = _get_engine().analyse(board, limit, multipv=multipv)
    except (chess.engine.EngineTerminatedError, chess.engine.EngineError):
        # The engine died (killed, OOM): drop it and retry once with a fresh one.
        _quit_engine()
        infos = _get_engine().analyse(board, limit, multipv=multipv)
    lines = []
    for info in infos:
        pv = info.get("pv") or []
        if not pv:
            continue
        score = info["score"].pov(board.turn)
        mate = score.mate()
        lines.append({
            "uci": pv[0].uci(),
            "cp": None if mate is not None else score.score(),
            "mate": mate,
            "pv": [m.uci() for m in pv[:MAX_PV_PLIES]],
        })
    return lines


def _worker_main(tasks, results) -> None:
    parent = multiprocessing.parent_process()
    try:
        while True:
            try:
                item = tasks.get(timeout=2)
            except queue.Empty:
                if parent is not None and not parent.is_alive():
                    break  # orphaned: the server that started us is gone
                continue
            if item is None:
                break
            batch, idx, fen, depth, multipv = item
            try:
                results.put((batch, idx, _analyse(fen, depth, multipv), None))
            except Exception as e:  # one bad position mustn't take the worker down
                results.put((batch, idx, [], f"{type(e).__name__}: {e}"))
    finally:
        _quit_engine()


class PoolBroken(Exception):
    pass


class _Pool:
    def __init__(self, workers: int):
        ctx = multiprocessing.get_context("spawn")
        self.workers = workers
        self.tasks = ctx.Queue()
        self.results = ctx.Queue()
        self.procs = [ctx.Process(target=_worker_main, args=(self.tasks, self.results), daemon=True)
                      for _ in range(workers)]
        for p in self.procs:
            p.start()
        self._batch = 0
        self._lock = threading.Lock()  # one batch at a time through a pool

    def alive(self) -> bool:
        return all(p.is_alive() for p in self.procs)

    def run(self, fens: list[str], depth: int, multipv: int) -> list[list[dict]]:
        with self._lock:
            self._batch += 1
            batch = self._batch
            for i, fen in enumerate(fens):
                self.tasks.put((batch, i, fen, depth, multipv))
            out: list[list[dict]] = [[] for _ in fens]
            got = 0
            deadline = time.monotonic() + max(60.0, 2 * len(fens) * STOCKFISH_MAX_SECONDS_PER_POSITION / self.workers)
            while got < len(fens):
                try:
                    b, idx, lines, error = self.results.get(timeout=2)
                except queue.Empty:
                    if not self.alive() or time.monotonic() > deadline:
                        raise PoolBroken("an analysis worker died or stalled")
                    continue
                if b != batch:
                    continue  # a straggler from an earlier, abandoned batch
                if error:
                    logger.warning(f"Analysis failed for {fens[idx]}: {error}")
                out[idx] = lines
                got += 1
            return out

    def close(self) -> None:
        for _ in self.procs:
            try:
                self.tasks.put(None)
            except Exception:
                pass
        for p in self.procs:
            p.join(timeout=3)
        for p in self.procs:
            if p.is_alive():
                p.terminate()


_pool: _Pool | None = None
_pool_lock = threading.Lock()


def _get_pool(workers: int) -> _Pool:
    global _pool
    with _pool_lock:
        if _pool is not None and (_pool.workers != workers or not _pool.alive()):
            _pool.close()
            _pool = None
        if _pool is None:
            _pool = _Pool(workers)
        return _pool


def shutdown() -> None:
    global _pool
    with _pool_lock:
        if _pool is not None:
            _pool.close()
            _pool = None
    _quit_engine()


atexit.register(shutdown)


def _analyse_in_process(fens: list[str], depth: int, multipv: int) -> list[list[dict]]:
    """Sequential fallback (workers=1): a throwaway engine, quit before
    returning — never left running in the calling process, whose exit it
    would otherwise block (see the module docstring)."""
    results = []
    try:
        for fen in fens:
            try:
                results.append(_analyse(fen, depth, multipv))
            except Exception as e:
                logger.warning(f"Analysis failed for {fen}: {e}")
                results.append([])
    finally:
        _quit_engine()
    return results


def analyse_many(fens: list[str], depth: int, multipv: int = 2, workers: int | None = None) -> list[list[dict]]:
    """Analyze every position in `fens` in parallel; results are in the same
    order as the input. A position that fails yields [] rather than sinking
    the whole batch."""
    global _pool
    if not fens:
        return []
    workers = workers or default_workers()
    if workers == 1 or len(fens) == 1:
        return _analyse_in_process(fens, depth, multipv)

    for attempt in (1, 2):
        pool = _get_pool(workers)
        try:
            return pool.run(fens, depth, multipv)
        except PoolBroken as e:
            logger.warning(f"{e}; restarting the analysis pool" + (" and retrying." if attempt == 1 else "."))
            with _pool_lock:
                if _pool is pool:
                    pool.close()
                    _pool = None
    return [[] for _ in fens]

"""Concurrent ingest pipeline for DedupSuite 2.0.

A producer thread pool hashes files and enqueues batched results; a consumer
thread writes to SQLite on a dedicated connection (WAL-safe). When ``db_path`` is
``None``, hashing still runs concurrently but rows are only collected in memory
(for duplicate grouping without a database).
"""

from __future__ import annotations

import sqlite3
import threading
import time
from concurrent.futures import ThreadPoolExecutor, as_completed
from pathlib import Path
from queue import Queue
from typing import Callable, List, Optional, Sequence, Tuple

import db_ingest
import ingest_kernel

Row = Tuple[Path, dict]

QUEUE_MAXSIZE = 32
BATCH_SIZE = 64
PRODUCER_WORKERS = 8
SENTINEL = object()


def _producer(
    paths: Sequence[Path],
    queue: Queue,
    *,
    stop_event: Optional[threading.Event],
    pause_event: Optional[threading.Event],
) -> None:
    batch: List[Row] = []
    with ThreadPoolExecutor(max_workers=PRODUCER_WORKERS) as pool:
        futures = {
            pool.submit(ingest_kernel.parse_metadata_and_hash, path): path
            for path in paths
        }
        for future in as_completed(futures):
            if stop_event and stop_event.is_set():
                break
            if pause_event:
                pause_event.wait()
            path = futures[future]
            batch.append((path, future.result()))
            if len(batch) >= BATCH_SIZE:
                queue.put(batch)
                batch = []
    if batch:
        queue.put(batch)
    queue.put(SENTINEL)


def _consumer_db(
    queue: Queue,
    db_path: Path,
    *,
    device_id: Optional[str],
    session_id: Optional[str],
) -> Tuple[int, List[Row]]:
    conn = sqlite3.connect(str(db_path), check_same_thread=False)
    collected: List[Row] = []
    try:
        db_ingest.configure_connection(conn)
        inserted = 0
        while True:
            item = queue.get()
            try:
                if item is SENTINEL:
                    break
                collected.extend(item)
                inserted += db_ingest.insert_batch(
                    conn,
                    item,
                    device_id=device_id,
                    session_id=session_id,
                )
                conn.commit()
            finally:
                queue.task_done()
        if inserted:
            db_ingest.finalize_index(conn)
    finally:
        conn.close()
    return inserted, collected


def _consumer_collect(queue: Queue) -> Tuple[int, List[Row]]:
    collected: List[Row] = []
    inserted = 0
    while True:
        item = queue.get()
        try:
            if item is SENTINEL:
                break
            collected.extend(item)
            inserted += sum(
                1 for _, rec in item if rec.get("status") == "success"
            )
        finally:
            queue.task_done()
    return inserted, collected


def run_pipeline(
    paths: Sequence[Path],
    db_path: Optional[Path] = None,
    *,
    device_id: Optional[str] = None,
    session_id: Optional[str] = None,
    stop_event: Optional[threading.Event] = None,
    pause_event: Optional[threading.Event] = None,
    progress_callback: Optional[Callable[[int, int, str], None]] = None,
) -> Tuple[float, int, List[Row]]:
    """Run the concurrent pipeline.

    Returns:
        ``(duration_sec, inserted_count, collected_rows)`` where ``collected_rows``
        holds every ``(path, record)`` batch element for downstream duplicate
        grouping.
    """
    if not paths:
        return 0.0, 0, []

    queue: Queue = Queue(maxsize=QUEUE_MAXSIZE)
    result_box: List[Tuple[int, List[Row]]] = []

    def consumer_wrapper() -> None:
        if db_path is not None:
            result_box.append(
                _consumer_db(
                    queue,
                    db_path,
                    device_id=device_id,
                    session_id=session_id,
                )
            )
        else:
            result_box.append(_consumer_collect(queue))

    producer = threading.Thread(
        target=_producer,
        args=(paths, queue),
        kwargs={
            "stop_event": stop_event,
            "pause_event": pause_event,
        },
    )
    consumer = threading.Thread(target=consumer_wrapper)

    start = time.perf_counter()
    producer.start()
    consumer.start()
    producer.join()
    consumer.join()
    duration = time.perf_counter() - start

    inserted, collected = result_box[0] if result_box else (0, [])
    if progress_callback:
        progress_callback(len(collected), len(paths), "Pipeline complete")

    return duration, inserted, collected

"""Concurrent ingest pipeline for DedupSuite 2.0.

A producer thread pool hashes files and enqueues batched results; a consumer
thread writes to SQLite on a dedicated connection (WAL-safe). When ``db_path`` is
``None``, hashing still runs concurrently but rows are only collected in memory
(for duplicate grouping without a database).
"""

from __future__ import annotations

import json
import sqlite3
import threading
import time
from concurrent.futures import ThreadPoolExecutor, as_completed
from pathlib import Path
from queue import Queue
from typing import Callable, List, Optional, Sequence, Tuple

import db_ingest
from db_ingest import TrialLimitExceededError
import ingest_kernel
from crypto_gate import get_crypto_gate

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


def _run_trial_gated_ingest(
    paths: Sequence[Path],
    db_path: Path,
    *,
    device_id: Optional[str],
    session_id: Optional[str],
    trial_limit: int = db_ingest.TRIAL_GOLDEN_LIMIT,
    stop_event: Optional[threading.Event] = None,
    pause_event: Optional[threading.Event] = None,
    progress_callback: Optional[Callable[[int, int, str], None]] = None,
) -> Tuple[float, int, List[Row]]:
    """Sequential ingest with pre-hash trial checks and per-row golden classification."""
    gate = get_crypto_gate()
    conn = sqlite3.connect(str(db_path), check_same_thread=False)
    collected: List[Row] = []
    inserted = 0
    batch_index = 0
    start = time.perf_counter()

    def _flush_batch(batch: List[Row]) -> None:
        nonlocal inserted, batch_index
        if not batch:
            return
        manifest = gate.build_batch_manifest(batch)
        row_count = len(manifest.get("entries", []))
        if row_count:
            signature = gate.sign_manifest(manifest)
            inserted += db_ingest.insert_batch_with_trial(
                conn,
                batch,
                device_id=device_id,
                session_id=session_id,
                trial_limit=trial_limit,
            )
            db_ingest.insert_batch_signature(
                conn,
                batch_index=batch_index,
                manifest_json=json.dumps(
                    manifest, sort_keys=True, separators=(",", ":")
                ),
                signature=signature,
                public_key=gate.public_key_bytes,
                row_count=row_count,
                created_at=manifest["signed_at"],
            )
            batch_index += 1
        conn.commit()

    try:
        db_ingest.configure_connection(conn)
        for index, path in enumerate(paths):
            if stop_event and stop_event.is_set():
                break
            if pause_event:
                pause_event.wait()
            db_ingest.assert_trial_capacity(conn, trial_limit)
            record = ingest_kernel.parse_metadata_and_hash(path)
            row = (path, record)
            collected.append(row)
            if record.get("status") == "success":
                _flush_batch([row])
            if progress_callback:
                progress_callback(len(collected), len(paths), f"Hashing: {path.name}")
        if inserted:
            db_ingest.finalize_index(conn)
    except TrialLimitExceededError as exc:
        conn.commit()
        if inserted:
            db_ingest.finalize_index(conn)
        raise TrialLimitExceededError(
            exc.message,
            inserted=inserted,
            collected=collected,
        ) from exc
    finally:
        conn.close()

    duration = time.perf_counter() - start
    return duration, inserted, collected


def _consumer_db(
    queue: Queue,
    db_path: Path,
    *,
    device_id: Optional[str],
    session_id: Optional[str],
    use_trial_gate: bool = False,
    trial_limit: int = db_ingest.TRIAL_GOLDEN_LIMIT,
) -> Tuple[int, List[Row]]:
    gate = get_crypto_gate()
    conn = sqlite3.connect(str(db_path), check_same_thread=False)
    collected: List[Row] = []
    try:
        db_ingest.configure_connection(conn)
        inserted = 0
        batch_index = 0
        while True:
            item = queue.get()
            try:
                if item is SENTINEL:
                    break
                collected.extend(item)
                manifest = gate.build_batch_manifest(item)
                row_count = len(manifest.get("entries", []))
                if row_count:
                    signature = gate.sign_manifest(manifest)
                    if use_trial_gate:
                        inserted += db_ingest.insert_batch_with_trial(
                            conn,
                            item,
                            device_id=device_id,
                            session_id=session_id,
                            trial_limit=trial_limit,
                        )
                    else:
                        inserted += db_ingest.insert_batch(
                            conn,
                            item,
                            device_id=device_id,
                            session_id=session_id,
                        )
                    db_ingest.insert_batch_signature(
                        conn,
                        batch_index=batch_index,
                        manifest_json=json.dumps(
                            manifest, sort_keys=True, separators=(",", ":")
                        ),
                        signature=signature,
                        public_key=gate.public_key_bytes,
                        row_count=row_count,
                        created_at=manifest["signed_at"],
                    )
                    batch_index += 1
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
    trial_golden_limit: Optional[int] = db_ingest.TRIAL_GOLDEN_LIMIT,
) -> Tuple[float, int, List[Row]]:
    """Run the concurrent pipeline.

    When ``db_path`` is set, opens (or creates) that SQLite file, bootstraps
    ``file_index`` and ``batch_signatures`` via :func:`db_ingest.configure_connection`,
    hashes with :data:`PRODUCER_WORKERS` (8) threads, and signs each committed batch.

    Args:
        paths: Absolute file paths under the production source tree.
        db_path: Absolute path to the production SQLite database, or ``None`` for
            in-memory collection only.

    Returns:
        ``(duration_sec, inserted_count, collected_rows)`` where ``collected_rows``
        holds every ``(path, record)`` batch element for downstream duplicate
        grouping.
    """
    if not paths:
        return 0.0, 0, []

    if db_path is not None and trial_golden_limit is not None:
        try:
            duration, inserted, collected = _run_trial_gated_ingest(
                paths,
                db_path,
                device_id=device_id,
                session_id=session_id,
                trial_limit=trial_golden_limit,
                stop_event=stop_event,
                pause_event=pause_event,
                progress_callback=progress_callback,
            )
        except TrialLimitExceededError as exc:
            if progress_callback:
                progress_callback(
                    len(exc.collected),
                    len(paths),
                    "Trial golden limit reached",
                )
            raise
        if progress_callback:
            progress_callback(len(collected), len(paths), "Pipeline complete")
        return duration, inserted, collected

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

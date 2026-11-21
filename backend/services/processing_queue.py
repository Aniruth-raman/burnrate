"""Concurrency-limited statement processing queue.

Shared by both the bulk upload endpoint and the folder watcher so that
at most MAX_CONCURRENT files are being parsed at any given time.
"""

import logging
from concurrent.futures import Future, ThreadPoolExecutor
from typing import Callable, Dict, Optional

from backend.models.database import SessionLocal

logger = logging.getLogger(__name__)

MAX_CONCURRENT = 10

_executor = ThreadPoolExecutor(max_workers=MAX_CONCURRENT, thread_name_prefix="stmt")


def _process_one(
    pdf_path: str,
    bank: Optional[str],
    manual_password: Optional[str],
    db_session_factory: Optional[Callable],
    source: str = "CC",
    original_upload_path: Optional[str] = None,
) -> Dict:
    """Run process_statement in a worker thread with its own DB session."""
    from backend.services.statement_processor import process_statement

    factory = db_session_factory or SessionLocal
    session = factory()
    try:
        return process_statement(
            pdf_path=pdf_path,
            bank=bank,
            db_session=session,
            manual_password=manual_password,
            source=source,
            original_upload_path=original_upload_path,
        )
    finally:
        session.close()


def submit(
    pdf_path: str,
    bank: Optional[str] = None,
    manual_password: Optional[str] = None,
    db_session_factory: Optional[Callable] = None,
    source: str = "CC",
    original_upload_path: Optional[str] = None,
) -> Future:
    """Submit a file for processing. Returns a Future so callers can wait."""
    return _executor.submit(
        _process_one,
        pdf_path,
        bank,
        manual_password,
        db_session_factory,
        source,
        original_upload_path,
    )


def run(fn: Callable, *args) -> Future:
    """Run any callable on the shared worker pool."""
    return _executor.submit(fn, *args)


def shutdown(wait: bool = True) -> None:
    """Gracefully shut down the worker pool."""
    _executor.shutdown(wait=wait)

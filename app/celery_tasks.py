"""
celery_tasks.py

This file contains Celery background tasks.

For now, we only have a simpel test task called ("ping") to prove
the celery_worker picks up and executes jobs end-to-end.

Later on follow the IDs-not-objects rule: pass an id, then re-fetch and
re-authorize inside the task — never trust that an enqueued id was permitted.
"""

import logging

from celery import shared_task


logger = logging.getLogger(__name__)


@shared_task(name="app.celery_tasks.ping")
def ping() -> str:
    """
    Dummy task: log, return a tiny non-sensitive result. No args, no DB.
    """
    logger.info("pong - ping task executed successfully by worker")

    return "pong"

"""Celery tasks for production batches. The logic lives in batch_flow.py so it can
also run in a plain thread when no Celery broker is available (local development)."""
import logging

from celery import shared_task

logger = logging.getLogger(__name__)


@shared_task
def label_batch(batch_id: int):
    """Label pass: buy each waiting order's label; layout follows on its own once
    every order is settled."""
    from .batch_flow import run_label_pass
    run_label_pass(batch_id)


@shared_task(bind=True, max_retries=1, default_retry_delay=30)
def layout_batch(self, batch_id: int):
    """Gang sheets + slip PDFs from the labeled orders. Free and repeatable."""
    from .batch_flow import run_layout
    run_layout(batch_id)

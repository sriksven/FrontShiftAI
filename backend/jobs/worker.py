import os
from celery import Celery
from celery.schedules import crontab

# Get Redis URL from environment or default to localhost
REDIS_URL = os.getenv("REDIS_URL", "redis://localhost:6379/0")

celery_app = Celery(
    "frontshiftai",
    broker=REDIS_URL,
    backend=REDIS_URL,
    include=["jobs.tasks"]
)

celery_app.conf.update(
    task_serializer="json",
    accept_content=["json"],
    result_serializer="json",
    timezone="UTC",
    enable_utc=True,
)

# Periodic tasks (run `celery beat` alongside the worker to schedule these).
celery_app.conf.beat_schedule = {
    "purge-stale-idempotency-records": {
        # Phase 0.5E: drop IdempotencyRecord rows >24h daily at 03:15 UTC.
        "task": "jobs.tasks.purge_stale_idempotency_records",
        "schedule": crontab(hour=3, minute=15),
    },
}

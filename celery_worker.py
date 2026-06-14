"""
celery_worker.py

This is the file celery uses to start the worker.

Create the Flask app by calling create_app(), so the celery instance
and its application context are fully configured, then create a `celery`
variable to expose `celery` as the worker target:

    celery -A celery_worker.celery worker --loglevel=info

Lives at the project root so the worker process starts where .env is, which
satisfies create_app()'s FLASK_ENV requirement. Celery Tasks register via create_app()
(it imports app.celery_tasks), so no extra import is needed here.

Note: create_app() eagerly loads the ML + LLM stack, so the Celery worker may take longer to start
and may print many logs. That is expected in current design. (it'll be improved by Stage 2/3 item).
"""

from app import create_app

flask_app = create_app()
celery = flask_app.extensions["celery"]
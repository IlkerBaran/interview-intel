"""
celery_worker.py

This is the file Celery uses to start the worker.

Create the Flask app by calling create_app(), so the Celery instance,
its application context, and its task registration are fully configured.
Then create a `celery` variable so Celery can use it as the worker target:

    celery -A celery_worker.celery worker --loglevel=info

Celery tasks register through create_app() because it imports app.celery_tasks,
so no extra import is needed here.

Email-only worker (Stage 2)
---------------------------
If the worker only needs to send email, you can skip loading the ML/LLM weights
by setting LOAD_MODELS=0:

    LOAD_MODELS=0 .venv/bin/python -m celery -A celery_worker.celery worker --pool=solo --loglevel=info

LOAD_MODELS=0 skips the model weights, which are the main memory-heavy part and
would otherwise multiply across worker processes and replicas (ADR-0008).
It does not stop the numpy/joblib import that still happens at module import time,
which is why --pool=solo is still used to avoid the macOS prefork fork-safety issue.
Removing that import is a separate later improvement.

The worker also reads SERVER_NAME from the environment so url_for(_external=True)
can build verification and reset links outside - of a request context (ADR-0006).

This file lives at the project root so the worker starts where .env is located,
which satisfies create_app()'s FLASK_ENV requirement.

Note: if LOAD_MODELS is not set to 0, create_app() will still eagerly load the
ML + LLM stack, so the worker may take longer to start and may print many logs.
That is expected in the current design and will be improved later.
"""

from app import create_app

flask_app = create_app()
celery = flask_app.extensions["celery"]
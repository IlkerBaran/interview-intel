"""
celery_app.py

Celery integration for the Flask application factory.

`celery_init_app(app)` creates and configures the Celery instance, binds tasks
to Flask's application context, and stores it on `app.extensions["celery"]`.

Tasks run inside `with app.app_context()` so they can use `current_app`,
`app.config`, and `db.session`. When the context exits, Flask-SQLAlchemy runs
its teardown cleanup, including `session.remove()`.

(verified against Flask-SQLAlchemy 3.1.1: extension.py:318 + 448) app-context teardown and
session cleanup logic.
"""

from celery import Celery, Task
from flask import Flask


def celery_init_app(app: Flask) -> Celery:
    class FlaskTask(Task):
        """
        Celery task base class that runs tasks inside the Flask app context
        """
        def __call__(self, *args, **kwargs):
            with app.app_context():
                return self.run(*args, **kwargs)

    celery_app = Celery(app.name, task_cls=FlaskTask)
    celery_app.config_from_object(app.config["CELERY"])
    celery_app.set_default()    # Makes @shared_task use this Celery app instance.
    app.extensions["celery"] = celery_app

    return celery_app



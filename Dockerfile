# syntax=docker/dockerfile:1

# Pin the Debian release so future Docker builds keep using the same base.
# Using `slim-trixie` avoids the generic `slim` tag changing underneath us.
# Avoid Alpine: scikit-learn has no compatible musllinux wheel for CPython 3.14,
# so pip would fall back to a source build with extra compiler dependencies.
FROM python:3.14.3-slim-trixie

ENV PYTHONDONTWRITEBYTECODE=1 \
    PYTHONUNBUFFERED=1 \
    PIP_NO_CACHE_DIR=1 \
    PIP_DISABLE_PIP_VERSION_CHECK=1

WORKDIR /app

# Install dependencies in a separate layer so Docker can reuse them when only
# application code changes instead of reinstalling the ML stack.
#
# Compatible manylinux wheels are available for the runtime dependencies, so no
# compiler toolchain is needed to build NumPy, SciPy, or scikit-learn from source.
COPY requirements.txt .
RUN pip install --no-cache-dir -r requirements.txt

# .dockerignore keeps the build context small by excluding local and
# development-only files, leaving only the files needed at runtime.
COPY . .

# Run as a non-root user; /tmp stays writable for Celery Beat schedule files.
RUN useradd --create-home --uid 10001 appuser && chown -R appuser:appuser /app
USER appuser

EXPOSE 8000

# Start as the web app by default. Compose overrides the command for
# the migration and Celery worker containers.
# Use the Flask app factory from the app package.
# `app:app` does not work because the package exports create_app(), not an app instance.
CMD ["gunicorn", "app:create_app()", \
     "--bind", "0.0.0.0:8000", \
     "--workers", "2", \
     "--access-logfile", "-", \
     "--error-logfile", "-"]
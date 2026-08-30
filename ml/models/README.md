# ml/models — trained model artifacts

**These files are committed to version control.** The application loads these files at runtime
and the Docker image includes them, so the committed versions are the source of truth.

## What is here

| File | Produced by | Loaded by |
|---|---|---|
| `category_model.pkl` | `ml/train.py` | `app/services/ml_service.py` |
| `urgency_model.pkl` | `ml/train.py` | `app/services/ml_service.py` |
| `job_field_model.pkl` | `ml/train.py` | `app/services/ml_service.py` |
| `job_field_threshold.json` | `ml/train.py` | `app/services/ml_service.py` |

`job_field_threshold.json` holds the abstain threshold that `train.py` derives from
the model it just trained. It is not optional bookkeeping: if the file is missing,
`ml_service` logs a warning and falls back to
`DEFAULT_JOB_FIELD_CONFIDENCE_THRESHOLD`, a fallback constant calibrated for a
different model setup, which can silently cause confident predictions to be labeled
`unclassified`. Keep it committed alongside the pickles it was derived from.

## Why they are committed

A fresh clone must be able to `docker compose up` and get working ML predictions
without a training step. `.dockerignore` therefore keeps `ml/models/` in the build
context — under the heading *"Keep ml/models/ because production needs the trained
models"* — while excluding the training-only assets (`ml/data/`, `ml/notebooks/`,
`ml/train.py`).

The ML worker treats missing models as fatal in production: `app/__init__.py`
re-raises `FileNotFoundError` when `FLASK_ENV=production`, so a container built
without these files fails at startup rather than serving degraded results.

## Regenerating them

```bash
python ml/train.py
```

`train.py` resolves its paths from `__file__`, so it can be run from any working
directory. It reads `ml/data/dataset.csv` (see `ml/data/README.md` for how to
generate that) and overwrites all four files above.

**Regenerating changes tracked files.** Commit the result — the running application
and the Docker image both depend on what is in the repository, so an uncommitted
retrain only exists on your machine.

`ml/train.py` is the authoritative training path. `ml/notebooks/02_train_models.ipynb`
also writes the three `.pkl` files but does **not** derive
`job_field_threshold.json`; training via the notebook alone leaves new models paired
with the previous model's threshold. See `ml/data/README.md`.

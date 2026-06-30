# 0008. Gate Eager ML/LLM Loading Behind LOAD_MODELS

## Status

Accepted

## Context

`create_app()` eagerly loads the full ML and LLM stack at startup — three scikit-learn
pipelines and the Anthropic client — so the web app has them ready on the first request.
Every process that runs the application builds it through `create_app()`, including Celery
workers.

Stage 2 introduces the first worker that needs none of that: an email-only worker. As
written, it would load the entire model stack just to send a verification email. That is
wasted memory, and it multiplies in exactly the wrong direction — model weights × prefork
child processes × replicas. The cost is worst precisely when scaling email workers up for
throughput, the moment the waste should be smallest.

## Decision

Gate the eager load behind a `LOAD_MODELS` environment flag, read inside `create_app()`.
Each process reads its own `LOAD_MODELS` at startup: the web app runs with `LOAD_MODELS=1`
(the default), the email worker with `LOAD_MODELS=0`. Same code, same factory, different
environment — no separate app build and no role branching at the call sites.

## Consequences

* An email worker boots without the model weights — the memory that would otherwise
multiply across child processes and replicas.
* Honest limit: this sheds the model *weights*, not the library *import*. `ml_service`
(and its top-level `numpy`/`joblib` import) is imported unconditionally by the app package,
so the C-extension import still happens at process start. That import is what forces the
email worker to run with `--pool=solo` to avoid the macOS prefork fork-safety crash. Fully
shedding the import — making `ml_service`/`llm_service` import lazily — is a larger change
deferred to Stage 3.
* This is the precondition for Stage 3's planned fast-email / slow-ML queue split:
load-shedding is the first half, queue routing (`-Q email` / `-Q ml`) the second. A future
`APP_ROLE` setting may subsume `LOAD_MODELS` once roles multiply.
* One more environment variable to set correctly. A web app misconfigured with
`LOAD_MODELS=0` would skip model loading and fail at the first prediction; the default is
`1`, so that failure mode requires an explicit opt-out.

"""
Stage 2 regression tests for app wiring.

The unread-count context processor must be safe outside a request context so
worker-rendered email templates do not crash. LOAD_MODELS must gate heavy
ML/LLM service loading.
"""

import config


def test_unread_count_guard_is_safe_without_request(app):
    from flask import render_template_string

    # App context only, no request context: same shape as worker-side rendering.
    out = render_template_string("{{ unread_count }}")

    assert out == "0"


def test_load_models_gate(monkeypatch):
    from app.services import llm_service as llm_mod
    from app.services import ml_service as ml_mod

    ml_calls = []
    llm_calls = []

    monkeypatch.setattr(ml_mod.ml_service, "load", lambda *a, **k: ml_calls.append(1))
    monkeypatch.setattr(llm_mod.llm_service, "load", lambda *a, **k: llm_calls.append(1))

    from app import create_app

    monkeypatch.setattr(config.TestingConfig, "LOAD_MODELS", False)
    create_app()

    assert ml_calls == []
    assert llm_calls == []

    monkeypatch.setattr(config.TestingConfig, "LOAD_MODELS", True)
    create_app()

    assert ml_calls == [1]
    assert llm_calls == [1]

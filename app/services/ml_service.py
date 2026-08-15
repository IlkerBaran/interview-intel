import json
import numpy as np
import re
import logging
import joblib
from pathlib import Path

logger = logging.getLogger(__name__)
BASE_DIR = Path(__file__).resolve().parent.parent.parent
MODELS_DIR = BASE_DIR / "ml" / "models"
THRESHOLD_FILE = MODELS_DIR / "job_field_threshold.json"

logger.debug("MODELS_DIR: %s (exists: %s)", MODELS_DIR, MODELS_DIR.exists())

# ≈≈≈≈≈≈ confidence threshold ≈≈≈≈≈≈
# Below this the job_field prediction is treated as uncertain and the label is
# replaced by JOB_FIELD_UNCLASSIFIED.
#
# The real value is DERIVED per model by ml/train.py and read from
# job_field_threshold.json; this constant is only the fallback for when that file
# is missing. Do not tune this number by hand — it thresholds a softmax over raw
# LinearSVC margins, whose scale changes with the data, the class count and the
# estimator. The previous hardcoded 0.30 was chosen for 8 classes and was still in
# place at 11, where it discarded 139 of 368 correct held-out predictions.
DEFAULT_JOB_FIELD_CONFIDENCE_THRESHOLD = 0.18

# Written in place of the label when the model is below the threshold. Chosen so
# the model can never emit it: 'general' used to serve this purpose but was also a
# real trainable class, so "General" on screen was ambiguous between "predicted
# general" and "was unsure". It is no longer a class at all.
JOB_FIELD_UNCLASSIFIED = "unclassified"


def _load_job_field_threshold():
    """Read the trainer-derived threshold, falling back to the constant."""
    try:
        value = json.loads(THRESHOLD_FILE.read_text())["job_field_confidence_threshold"]
        logger.info("job_field threshold %.3f (derived, from %s)", value, THRESHOLD_FILE.name)
        return float(value)
    except (OSError, ValueError, KeyError, TypeError) as e:
        logger.warning(
            "Could not read %s (%s) — falling back to %.3f. Run python ml/train.py "
            "to regenerate it.",
            THRESHOLD_FILE.name, type(e).__name__,
            DEFAULT_JOB_FIELD_CONFIDENCE_THRESHOLD,
        )
        return DEFAULT_JOB_FIELD_CONFIDENCE_THRESHOLD


JOB_FIELD_CONFIDENCE_THRESHOLD = _load_job_field_threshold()

class MlService:
    """
    Loads trained scikit-learn pipelines once at app startup
    and exposes a single predict() interface.

    Models:
        category_model.pkl  → LogisticRegression
        urgency_model.pkl   → LogisticRegression
        job_field_model.pkl → LinearSVC

    Usage:
        ml_service.load()          # called once in create_app()
        result = ml_service.predict(text)
    """
    def __init__(self):
        self._category_model = None
        self._urgency_model = None
        self._job_field_model = None
        self._loaded = False

    # ≈≈≈≈≈ public method ≈≈≈≈≈
    def load(self):
        """
        Load all three models from disk.
        Called once in create_app() — fails loud if models missing.
        """
        try:
            self._category_model = joblib.load(MODELS_DIR / "category_model.pkl")
            self._urgency_model = joblib.load(MODELS_DIR / "urgency_model.pkl")
            self._job_field_model = joblib.load(MODELS_DIR / "job_field_model.pkl")
            self._loaded = True
            logger.info("Ml models loaded from: %s", MODELS_DIR)

        except FileNotFoundError as e:
            logger.error("Model file not found: %s", e)
            logger.error("Run python ml/train.py to generate them")
            raise

        # unknown error
        except Exception:
            logger.exception("Unexpected error loading Ml models")
            raise

    def predict(self, text):
        """
        Run all model classifiers on input text.

        :param text:
            Raw email string.
        :return:
            Dictionary with predictions and confidence scores.
            Returns empty result dictionary if models not loaded or text empty
        """
        # Guard clauses
        if not self._loaded:
            logger.warning("predict() called before models loaded")
            return self._empty_result()

        if not text or not text.strip():
            logger.warning("Empty text passed to predict()")
            return self._empty_result()

        try:
            normalized = self._normalize(text)

            # ≈≈≈≈ category prediction ≈≈≈≈
            category        = self._category_model.predict([normalized])[0]
            category_conf   = self._get_confidence(
                self._category_model, normalized
            )

            # ≈≈≈≈≈ urgency prediction ≈≈≈≈≈
            urgency         = self._urgency_model.predict([normalized])[0]
            urgency_conf    = self._get_confidence(
                self._urgency_model, normalized
            )

            # ≈≈≈≈ job field prediction ≈≈≈≈
            job_field       = self._job_field_model.predict([normalized])[0]
            job_field_conf  = self._get_confidence(
                self._job_field_model, normalized
            )

            # ≈≈≈≈ low confidence → abstain ≈≈≈≈
            # if model is uncertain about job field, don't guess
            # The score goes with the label: a NULL confidence is the template's signal
            # for "substituted", as opposed to a real prediction.
            if job_field_conf < JOB_FIELD_CONFIDENCE_THRESHOLD:
                logger.debug(
                    "Job field below threshold (%s at %.3f) — substituting '%s' "
                    "and dropping the score",
                    job_field,
                    job_field_conf,
                    JOB_FIELD_UNCLASSIFIED,
                    extra={
                        "job_field": job_field,
                        "job_field_conf": job_field_conf,
                        "job_field_threshold": JOB_FIELD_CONFIDENCE_THRESHOLD,
                    }
                )
                job_field = JOB_FIELD_UNCLASSIFIED
                job_field_conf = None

            return {
                "category":             category,
                "category_conf":        category_conf,
                "urgency":              urgency,
                "urgency_conf":         urgency_conf,
                "job_field":            job_field,
                "job_field_conf":       job_field_conf
            }

        except Exception:
            logger.exception("Ml prediction failed")
            return self._empty_result()

    @property
    def is_loaded(self):
        """check if models are ready"""
        return self._loaded

    # ≈≈≈≈ Private methods ≈≈≈≈
    def _normalize(self, text):
        """
        Normalize input text before prediction
        it matches the normalization applied during the training
        """
        text = text.strip()
        text = re.sub(r"\s+", " ", text)
        return text

    def _get_confidence(self, model, text):
        """
        Confidence for whichever estimator train.py selected for this head.

        Do NOT reintroduce a per-head assumption here. train.py picks the best of
        LogisticRegression / MultinomialNB / LinearSVC independently per target,
        so which estimator backs which head changes between retrains. Hardcoding
        predict_proba for category once cost us every prediction in the app: the
        retrain chose LinearSVC, predict() raised AttributeError, the broad except
        swallowed it, and predict() returned an empty result for every message.

        predict_proba  -> a real probability over the class set.
        decision_function -> softmax over raw margins. This is NOT calibrated and
        is not a probability; see the Field badge in show_message.html, which
        deliberately shows no percentage for job_field because of it.
        """
        if hasattr(model, "predict_proba"):
            return round(float(max(model.predict_proba([text])[0])), 3)

        scores     = np.atleast_1d(model.decision_function([text])[0])
        exp_scores = np.exp(scores - np.max(scores))   # subtract max for stability
        return round(float(exp_scores.max() / exp_scores.sum()), 3)

    def _empty_result(self):
        """
        Safe fallback when prediction cannot run.
        Workflow service handles None values gracefully.
        """
        return {
            "category": None,
            "category_confidence": None,
            "urgency": None,
            "urgency_confidence": None,
            "job_field": None,
            "job_field_confidence": None
        }

# ≈≈≈≈ singleton pattern ≈≈≈≈
ml_service = MlService()

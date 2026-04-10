import numpy as np
import re
import logging
import joblib
from pathlib import Path

logger = logging.getLogger(__name__)
BASE_DIR = Path(__file__).resolve().parent.parent.parent
MODELS_DIR = BASE_DIR / "ml" / "models"

logger.debug("MODELS_DIR: %s (exists: %s)", MODELS_DIR, MODELS_DIR.exists())

# ≈≈≈≈≈≈ confidence threshold ≈≈≈≈≈≈
# below this we consider the prediction uncertain
JOB_FIELD_CONFIDENCE_THRESHOLD = 0.30

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
            category_conf   = self._get_proba_confidence(
                self._category_model, normalized
            )

            # ≈≈≈≈≈ urgency prediction ≈≈≈≈≈
            urgency         = self._urgency_model.predict([normalized])[0]
            urgency_conf    = self._get_proba_confidence(
                self._urgency_model, normalized
            )

            # ≈≈≈≈ job field prediction ≈≈≈≈
            # LinearSVC uses decision_function — no predict_proba
            job_field       = self._job_field_model.predict([normalized])[0]
            job_field_conf  = self._get_svc_confidence(
                self._job_field_model, normalized
            )

            # ≈≈≈≈ low confidence → return general ≈≈≈≈
            # if model is uncertain about job field, don't guess
            if job_field_conf < JOB_FIELD_CONFIDENCE_THRESHOLD:
                logger.debug("Low job field confidence (%.3f) - return general", job_field_conf)
                job_field = "general"

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

    def _get_proba_confidence(self, model, text):
        """
        Get confidence score from models that support predict_proba.
        Only LogisticRegression and (MultinomialNB(not in-use)) support this.
        Returns max probability rounded to 3 decimal places.
        """
        proba = model.predict_proba([text])[0]
        return round(float(max(proba)), 3)

    def _get_svc_confidence(self, model, text):
        """
        Get confidence proxy from LinearSVC using decision_function.
        Uses softmax-style normalization over positive scores only.
        """

        scores  = model.decision_function([text])[0]
        # softmax normalization — much more meaningful than raw division
        exp_scores  = np.exp(scores - np.max(scores))  # subtract max for stability
        confidence  = float(exp_scores.max() / exp_scores.sum())
        return round(confidence, 3)

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






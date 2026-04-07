"""
ml/train.py
Run this to regenerate all three models from the dataset.
Usage: python ml/train.py
"""

import logging
import joblib
import pandas as pd
from pathlib import Path
from sklearn.model_selection import train_test_split
from sklearn.pipeline import Pipeline
from sklearn.feature_extraction.text import TfidfVectorizer
from sklearn.linear_model import LogisticRegression
from sklearn.naive_bayes import MultinomialNB
from sklearn.svm import LinearSVC
from sklearn.metrics import accuracy_score, classification_report

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s — %(message)s"
)
logger = logging.getLogger(__name__)

BASE_DIR    = Path(__file__).resolve().parent
file_path   = BASE_DIR / "data"   / "dataset.csv"
models_dir  = BASE_DIR / "models"


class ModelTrainer:
    """
    Loads dataset, trains best classifier per target label,
    and saves each model as a .pkl file.

    Targets:    category, urgency, job_field
    Models: LogisticRegression, MultinomialNB, LinearSVC
    Selection:  highest accuracy on stratified 80/20 split
    """

    def __init__(self):
        self.df                   = None
        self.X                    = None
        self.y_category           = None
        self.y_urgency            = None
        self.y_job_field          = None
        self.best_category_name   = None
        self.best_category_model  = None
        self.best_urgency_name    = None
        self.best_urgency_model   = None
        self.best_job_field_name  = None
        self.best_job_field_model = None

    def load_data(self):
        """Load and normalize dataset from CSV."""
        logger.info("Loading dataset from %s", file_path)

        if not file_path.exists():
            raise FileNotFoundError(
                f"Dataset not found at {file_path}. "
                "Run the dataset generator notebook first."
            )

        self.df = pd.read_csv(file_path)
        self.df["text"] = (
            self.df["text"]
            .str.strip()
            .str.replace(r"\s+", " ", regex=True)
        )

        self.X          = self.df["text"]
        self.y_category = self.df["category"]
        self.y_urgency  = self.df["urgency"]
        self.y_job_field = self.df["job_field"]

        logger.info("Rows: %d", len(self.df))
        logger.info("Features: Email text only")
        logger.info("Labels: category | urgency | job_field")
        return self

    def _build_pipeline(self, clf):
        """Wrap a classifier in a TF-IDF pipeline."""
        return Pipeline([
            ("tfidf", TfidfVectorizer(
                ngram_range=(1, 2),
                max_features=5000,
                min_df=1,
                sublinear_tf=True,
            )),
            ("clf", clf)
        ])

    def _evaluate_models(self, X_train, X_test, y_train, y_test, target_label):
        """
        Train all candidate models on the same split.
        Print classification report for each.
        Return the best pipeline by accuracy.
        """
        models = {
            "LogisticRegression": LogisticRegression(max_iter=1000, class_weight="balanced"),
            "MultinomialNB":      MultinomialNB(),
            "LinearSVC":          LinearSVC(max_iter=2000, class_weight="balanced"),
        }

        best_model, best_accuracy, best_pipeline = None, 0, None

        logger.info("\n%s", "=" * 60)
        logger.info("Target: %s", target_label.upper())
        logger.info("%s", "=" * 60)

        for name, clf in models.items():
            pipeline = self._build_pipeline(clf)
            pipeline.fit(X_train, y_train)

            PRED     = pipeline.predict(X_test)
            accuracy = accuracy_score(y_test, PRED)

            print(f"\n➡️{name} Accuracy is: {accuracy:.3f}")
            print(classification_report(y_test, PRED))

            if accuracy > best_accuracy:
                best_model, best_accuracy, best_pipeline = name, accuracy, pipeline

        logger.info("Best For %s: %s (%.3f)", target_label, best_model, best_accuracy)
        return best_model, best_pipeline

    def train_all(self):
        """
        Train and save models for all three target labels.
        Uses separate stratified splits per target — same design as notebook.
        """
        if self.df is None:
            raise RuntimeError("Call load_data() before train_all()")

        # ── category split ──
        X_train_cat, X_test_cat, y_cat_train, y_cat_test = train_test_split(
            self.X, self.y_category,
            test_size=0.2, random_state=42, stratify=self.y_category
        )

        # ── urgency split ──
        X_train_urg, X_test_urg, y_urg_train, y_urg_test = train_test_split(
            self.X, self.y_urgency,
            test_size=0.2, random_state=42, stratify=self.y_urgency
        )

        # ── job_field split ──
        X_train_job_field, X_test_job_field, y_job_field_train, y_job_field_test = train_test_split(
            self.X, self.y_job_field,
            test_size=0.2, random_state=42, stratify=self.y_job_field
        )

        logger.info("Category train/test: %d / %d", len(X_train_cat), len(X_test_cat))
        logger.info("Urgency train/test: %d / %d", len(X_train_urg), len(X_test_urg))
        logger.info("Job field train/test: %d / %d", len(X_train_job_field), len(X_test_job_field))

        # ── train each target ──
        self.best_category_name, self.best_category_model = self._evaluate_models(
            X_train_cat, X_test_cat, y_cat_train, y_cat_test, "category"
        )

        self.best_urgency_name, self.best_urgency_model = self._evaluate_models(
            X_train_urg, X_test_urg, y_urg_train, y_urg_test, "urgency"
        )

        self.best_job_field_name, self.best_job_field_model = self._evaluate_models(
            X_train_job_field, X_test_job_field, y_job_field_train, y_job_field_test, "job_field"
        )

        return self

    def save_models(self):
        """Save all three best models as .pkl files."""
        if self.best_category_model is None:
            raise RuntimeError("Call train_all() before save_models()")

        models_dir.mkdir(exist_ok=True)

        joblib.dump(self.best_category_model,  models_dir / "category_model.pkl")
        joblib.dump(self.best_urgency_model,   models_dir / "urgency_model.pkl")
        joblib.dump(self.best_job_field_model, models_dir / "job_field_model.pkl")

        logger.info("category_model.pkl:  %s", self.best_category_name)
        logger.info("urgency_model.pkl:   %s", self.best_urgency_name)
        logger.info("job_field_model.pkl: %s", self.best_job_field_name)
        logger.info("Saved to: %s", models_dir)
        return self


if __name__ == "__main__":
    ModelTrainer().load_data().train_all().save_models()
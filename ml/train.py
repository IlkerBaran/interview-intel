"""
ml/train.py
Run this to regenerate all three models from the dataset.
Usage: python ml/train.py
"""

import logging
import joblib
import numpy as np
import pandas as pd
from pathlib import Path
from sklearn.model_selection import StratifiedGroupKFold, cross_val_score
from sklearn.pipeline import Pipeline
from sklearn.feature_extraction.text import TfidfVectorizer
from sklearn.metrics.pairwise import cosine_similarity
from sklearn.linear_model import LogisticRegression
from sklearn.naive_bayes import MultinomialNB
from sklearn.svm import LinearSVC
from sklearn.metrics import accuracy_score, f1_score, classification_report

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s — %(message)s"
)
logger = logging.getLogger(__name__)

BASE_DIR    = Path(__file__).resolve().parent
file_path   = BASE_DIR / "data"   / "dataset.csv"
models_dir  = BASE_DIR / "models"

TARGETS = ("category", "urgency", "job_field")

# ── evaluation protocol ──
# One shared grouped split for all three heads (see _make_split docstring).
TEST_FRACTION_SPLITS = 5   # StratifiedGroupKFold(5) -> first fold is the ~20% held-out test
SELECTION_FOLDS      = 4   # inner CV on the TRAIN portion only; test is never touched
SELECTION_METRIC     = "f1_macro"
RANDOM_STATE         = 42

# ── fallback grouping ──
# Only used when the dataset has no template_id column. Rows are single-linkage
# clustered at this cosine; see _derive_groups for why this is second best.
NEAR_DUP_COSINE = 0.80

# Memorisation warning thresholds — tuned to be loud, not precise.
GAP_WARN        = 0.03    # permuted-group CV minus true-group CV
NEIGHBOUR_WARN  = 0.70    # median test->train nearest-neighbour cosine


class ModelTrainer:
    """
    Loads dataset, trains best classifier per target label,
    and saves each model as a .pkl file.

    Targets:    category, urgency, job_field
    Models:     LogisticRegression, MultinomialNB, LinearSVC
    Selection:  highest macro F1 under grouped cross-validation on the TRAIN
                portion; the held-out test set is never used to choose anything.
    """

    def __init__(self):
        self.df           = None
        self.groups       = None
        self.group_by     = None     # "template_id" or "near-duplicate clustering"
        self.train_df     = None
        self.test_df      = None
        self.train_groups = None
        self.best         = {}       # target -> (model_name, fitted_pipeline)

    # ══════════════════════════════════════════════════════════════════════
    # data
    # ══════════════════════════════════════════════════════════════════════
    def load_data(self):
        """Load and normalize dataset from CSV, establish grouping, then split."""
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

        self.groups, self.group_by = self._derive_groups(self.df)

        sizes = pd.Series(self.groups).value_counts()
        logger.info("Rows: %d", len(self.df))
        logger.info("Features: Email text only")
        logger.info("Labels: %s", " | ".join(TARGETS))
        logger.info(
            "Grouping: %s — %d groups, %.1f rows/group (min %d, median %d, max %d)",
            self.group_by, len(sizes), sizes.mean(),
            sizes.min(), int(sizes.median()), sizes.max(),
        )
        return self._make_split()

    def _derive_groups(self, df):
        """
        Establish the unit that must not straddle train and test.

        PREFERRED: a template_id column emitted by the generator. Rows sharing a
        template are fills of one sentence; splitting them puts a paraphrase of a
        training row into the test set.

        FALLBACK: single-linkage clustering at NEAR_DUP_COSINE. Measured against
        the true partition, clusters never merge two templates, but they FRAGMENT
        them — at 0.80, 61 of 72 templates broke into ~10 clusters each. So this
        catches near-identical twins and nothing more: same-template rows still
        land on both sides. It is a floor, not the guarantee. Add template_id.
        """
        if "template_id" in df.columns:
            return pd.factorize(df["template_id"])[0], "template_id"

        logger.warning(
            "No template_id column — falling back to near-duplicate clustering at "
            "cosine>=%.2f. This UNDER-GROUPS: it separates twins but not all rows "
            "sharing a template, so scores below remain optimistic. Emit "
            "template_id from the generator.", NEAR_DUP_COSINE,
        )
        vec = TfidfVectorizer(ngram_range=(1, 2), max_features=5000, sublinear_tf=True)
        adj = cosine_similarity(vec.fit_transform(df["text"])) >= NEAR_DUP_COSINE

        n, labels, cur = len(df), -np.ones(len(df), dtype=int), 0
        for i in range(n):
            if labels[i] < 0:
                stack, labels[i] = [i], cur
                while stack:
                    j = stack.pop()
                    for k in np.where(adj[j] & (labels < 0))[0]:
                        labels[k] = cur
                        stack.append(k)
                cur += 1
        return labels, f"near-duplicate clustering (cosine>={NEAR_DUP_COSINE})"

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

    def _candidates(self):
        """Fresh, unfitted candidates. Callable so every fold gets a clean model."""
        return {
            "LogisticRegression": lambda: LogisticRegression(max_iter=1000, class_weight="balanced"),
            "MultinomialNB":      lambda: MultinomialNB(),
            "LinearSVC":          lambda: LinearSVC(max_iter=2000, class_weight="balanced"),
        }

    # ══════════════════════════════════════════════════════════════════════
    # split
    # ══════════════════════════════════════════════════════════════════════
    def _make_split(self):
        """
        Use one shared train/test split for category, urgency, and job_field.

        Before, each one made its own split. That was not direct leakage because each
        model was separate, but it meant the same email could be training data for one
        model and test data for another.

        Now all three use the same split, so a test email is actually held out from the
        whole system.

        The split is stratified by category because each template belongs to one
        category. urgency and job_field cannot also be stratified in the same split, so
        their train/test distributions are printed instead.
        """
        splitter = StratifiedGroupKFold(
            n_splits=TEST_FRACTION_SPLITS, shuffle=True, random_state=RANDOM_STATE
        )
        train_idx, test_idx = next(
            splitter.split(self.df["text"], self.df["category"], groups=self.groups)
        )
        self.train_df = self.df.iloc[train_idx].reset_index(drop=True)
        self.test_df  = self.df.iloc[test_idx].reset_index(drop=True)
        self.train_groups = self.groups[train_idx]

        n_shared = len(set(self.groups[train_idx]) & set(self.groups[test_idx]))
        assert n_shared == 0, f"{n_shared} groups straddle train and test"

        logger.info("\n%s", "=" * 70)
        logger.info("SPLIT — one shared partition, grouped by %s", self.group_by)
        logger.info("%s", "=" * 70)
        logger.info(
            "train %d rows / %d groups   held-out test %d rows / %d groups   "
            "(groups shared: 0)",
            len(self.train_df), len(set(self.groups[train_idx])),
            len(self.test_df),  len(set(self.groups[test_idx])),
        )
        for target in TARGETS:
            tr = self.train_df[target].value_counts(normalize=True)
            te = self.test_df[target].value_counts(normalize=True)
            drift = ", ".join(
                f"{k} {tr.get(k, 0) * 100:.0f}/{te.get(k, 0) * 100:.0f}"
                for k in sorted(self.df[target].unique())
            )
            logger.info("  %-10s train/test %%: %s", target, drift)
        return self

    # ══════════════════════════════════════════════════════════════════════
    # memorisation diagnostics
    # ══════════════════════════════════════════════════════════════════════
    def report_memorisation(self):
        """
        Make it visible when a score is measuring recall of the training set.

        Two independent probes:

        1. NEIGHBOUR — cosine from each test row to its nearest train row. High
           values mean the test set contains paraphrases of things already seen,
           whatever the split claims.

        2. PERMUTED-GROUP CONTROL — the same grouped CV re-run with the group
           labels randomly permuted. That destroys the grouping while keeping fold
           count and sizes identical, so the difference isolates what grouping is
           worth. A large positive gap means the ungrouped number was inflated.
           A gap near zero means grouping was not what was propping the score up —
           which is NOT a clean bill of health: a model can generalise across
           templates and still fail on real email, and only real held-out email
           can tell you that.
        """
        logger.info("\n%s", "=" * 70)
        logger.info("MEMORISATION DIAGNOSTICS")
        logger.info("%s", "=" * 70)

        vec = TfidfVectorizer(ngram_range=(1, 2), max_features=5000, sublinear_tf=True)
        vec.fit(self.train_df["text"])
        sim = cosine_similarity(
            vec.transform(self.test_df["text"]), vec.transform(self.train_df["text"])
        ).max(axis=1)
        logger.info(
            "test -> nearest train row cosine: median %.3f | >0.90 %.1f%% | "
            ">0.80 %.1f%% | >0.70 %.1f%%",
            np.median(sim), (sim > 0.90).mean() * 100,
            (sim > 0.80).mean() * 100, (sim > 0.70).mean() * 100,
        )
        if np.median(sim) > NEIGHBOUR_WARN:
            logger.warning(
                "  ^ median above %.2f — the held-out set is largely paraphrase of "
                "train. Scores below are optimistic regardless of grouping.",
                NEIGHBOUR_WARN,
            )

        rng = np.random.default_rng(RANDOM_STATE)
        fake = rng.permutation(self.train_groups)
        logger.info("")
        logger.info("%-11s %-20s %8s %8s %8s", "head", "model", "grouped", "permuted", "gap")
        for target in TARGETS:
            y = self.train_df[target]
            for name, make in self._candidates().items():
                true_cv = self._cv(make, self.train_df["text"], y, self.train_groups)
                perm_cv = self._cv(make, self.train_df["text"], y, fake)
                gap = perm_cv - true_cv
                flag = "  <-- MEMORISATION" if gap > GAP_WARN else ""
                logger.info(
                    "%-11s %-20s %8.3f %8.3f %+8.3f%s",
                    target, name, true_cv, perm_cv, gap, flag,
                )
        return self

    def _cv(self, make, X, y, groups):
        """Mean SELECTION_METRIC under grouped CV. Never sees the test set."""
        return cross_val_score(
            self._build_pipeline(make()), X, y,
            cv=StratifiedGroupKFold(
                n_splits=SELECTION_FOLDS, shuffle=True, random_state=RANDOM_STATE
            ),
            groups=groups,
            scoring=SELECTION_METRIC,
        ).mean()

    # ══════════════════════════════════════════════════════════════════════
    # select + evaluate
    # ══════════════════════════════════════════════════════════════════════
    def train_all(self):
        """
        For each target: choose the model by grouped CV on TRAIN, refit it on all
        of TRAIN, then report per-class metrics on the held-out test set.

        The old code chose the winner by accuracy on the same 20% it then reported,
        so the quoted figure was the max of three noisy numbers on the data that
        picked it. Selection and reporting are now disjoint.
        """
        if self.df is None:
            raise RuntimeError("Call load_data() before train_all()")

        for target in TARGETS:
            logger.info("\n%s", "=" * 70)
            logger.info("TARGET: %s", target.upper())
            logger.info("%s", "=" * 70)

            y_train, y_test = self.train_df[target], self.test_df[target]

            logger.info("selection — grouped CV on train (%s), test untouched:",
                        SELECTION_METRIC)
            scores = {}
            for name, make in self._candidates().items():
                scores[name] = self._cv(make, self.train_df["text"], y_train, self.train_groups)
                logger.info("    %-20s %.3f", name, scores[name])

            best_name = max(scores, key=scores.get)
            runner_up = sorted(scores.values())[-2]
            logger.info(
                "  winner: %s (%.3f, +%.3f over next)",
                best_name, scores[best_name], scores[best_name] - runner_up,
            )
            if scores[best_name] - runner_up < 0.02:
                logger.warning(
                    "  ^ margin under 0.02 — the winner is within noise of the "
                    "runner-up; do not read this as one model beating another."
                )

            pipeline = self._build_pipeline(self._candidates()[best_name]())
            pipeline.fit(self.train_df["text"], y_train)
            pred = pipeline.predict(self.test_df["text"])

            acc = accuracy_score(y_test, pred)
            macro = f1_score(y_test, pred, average="macro")
            logger.info(
                "\n  HELD-OUT TEST — accuracy %.3f | macro F1 %.3f | CV->test drift %+.3f",
                acc, macro, macro - scores[best_name],
            )
            print(classification_report(y_test, pred, zero_division=0))

            labels = sorted(y_test.unique())
            per_class = f1_score(y_test, pred, average=None, labels=labels)
            lo, hi = int(np.argmin(per_class)), int(np.argmax(per_class))
            spread = per_class[hi] - per_class[lo]
            logger.info(
                "  per-class F1: %.3f (%s) .. %.3f (%s), spread %.3f",
                per_class[lo], labels[lo], per_class[hi], labels[hi], spread,
            )
            if spread > 0.15:
                logger.info("  ^ the macro average is smoothing over a weak class")
            if per_class[lo] < 0.50:
                logger.warning(
                    "  ^ %s is below 0.50 F1 — not usable in production", labels[lo]
                )

            self.best[target] = (best_name, pipeline)

        return self

    # ══════════════════════════════════════════════════════════════════════
    # save
    # ══════════════════════════════════════════════════════════════════════
    def save_models(self):
        """Save all three best models as .pkl files."""
        if not self.best:
            raise RuntimeError("Call train_all() before save_models()")

        models_dir.mkdir(exist_ok=True)
        for target, (name, pipeline) in self.best.items():
            joblib.dump(pipeline, models_dir / f"{target}_model.pkl")
            logger.info("%s_model.pkl: %s", target, name)

        logger.warning(
            "Saved models are fitted on the TRAIN portion only (%d of %d rows), so "
            "the reported held-out scores describe the artefacts actually shipped.",
            len(self.train_df), len(self.df),
        )
        logger.info("Saved to: %s", models_dir)
        return self


if __name__ == "__main__":
    (ModelTrainer()
     .load_data()
     .report_memorisation()
     .train_all()
     .save_models())

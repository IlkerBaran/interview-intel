# ml/data — training and evaluation data

The two files here have different roles and different version-control status. Only
one of them is generated.

| File | Tracked? | What it is |
|---|---|---|
| `real_emails.csv` | **committed** | A small set of real emails used as a held-out sanity check during training. Hand-curated, never regenerated. |
| `dataset.csv` | **gitignored** | The synthetic training set. Regenerated from a notebook; absent from a fresh clone. |

`.gitignore` ignores `ml/data/dataset.csv` specifically — not the directory. Do not
delete or overwrite `real_emails.csv` expecting it to come back from a generator;
nothing regenerates it.

Neither file ships in the Docker image: `.dockerignore` excludes `ml/data/` entirely,
because the container only needs the trained models in `ml/models/`.

## The pipeline

```
ml/notebooks/01_generate_synthetic_dataset.ipynb  →  ml/data/dataset.csv
                                                          ↓
                              python ml/train.py  →  ml/models/*.pkl
                                                  +  ml/models/job_field_threshold.json
```

A fresh clone has no `dataset.csv`, so `ml/train.py` will fail until step 1 has run.
That is expected — the committed models in `ml/models/` mean you only need this
pipeline when you actually want to retrain.

## Step 1 — generate the dataset

Run `ml/notebooks/01_generate_synthetic_dataset.ipynb`.

**The kernel's working directory must be `ml/notebooks/`.** The notebook computes its
output path as:

```python
BASE_DIR = Path().resolve().parent      # ← resolves against the KERNEL's cwd
file_path = BASE_DIR / "data" / "dataset.csv"
```

`Path()` is the current working directory, not the notebook's location, so the
notebook only writes to `ml/data/` when its kernel started in `ml/notebooks/`.

* Opening the notebook in Jupyter Lab/Notebook is fine — the kernel starts in the
  notebook's own directory, even if you launched Jupyter from the repository root.
* Executing headlessly from the repository root is **not** fine.
  `jupyter nbconvert --execute` inherits the shell's working directory, so `BASE_DIR`
  becomes the repository's *parent* and the dataset is written outside the repo. It
  fails silently — there is no error, just a file in the wrong place. If you need a
  headless run, `cd ml/notebooks` first.

The same applies to `02_train_models.ipynb`, which computes `BASE_DIR` identically.

## Step 2 — train the models

```bash
python ml/train.py
```

**`ml/train.py` is the authoritative training path.** Unlike the notebooks it resolves
paths from `__file__`, so it works from any working directory, and it is the only
thing that derives `job_field_threshold.json`.

### The notebook-02 trap

`ml/notebooks/02_train_models.ipynb` also trains and writes the three `.pkl` files,
but it does **not** write `job_field_threshold.json`. Training with the notebook alone
therefore leaves newly trained models sitting next to the *previous* model's threshold.

That threshold is a cutoff over raw LinearSVC margins, whose scale shifts with the
data and the class count, so a stale value does not raise an error — it quietly
mislabels confident `job_field` predictions as `unclassified`. Use `ml/train.py` when
you intend to update the models; keep notebook 02 for exploration.

See `ml/models/README.md` for what the trained artifacts are and why they are
committed.

"""Model lab: train, evaluate, save and activate win-probability models.

Every model predicts P(player 1 wins) from the features in dataset.FEATURES.
Models are trained on earlier years and tested on later ones (a time split), so
the test scores are an honest preview of how a model does on matches it hasn't
seen. Saved models live in models/ and one of them is "active": that's the one
the H2H, Matches and Billy's Model pages use.

All registry changes go through LOCK, so a click on "Use"/"Delete" can't race the
background engine's retraining.
"""
import io
import json
import re
import threading
import time
import traceback

import joblib
import numpy as np
import pandas as pd
from sklearn.ensemble import HistGradientBoostingClassifier, RandomForestClassifier
from sklearn.inspection import permutation_importance
from sklearn.linear_model import LogisticRegression
from sklearn.metrics import brier_score_loss, log_loss, roc_auc_score
from sklearn.neural_network import MLPClassifier
from sklearn.pipeline import make_pipeline
from sklearn.preprocessing import StandardScaler

import config
import dataset

MODEL_DIR = config.MODEL_DIR
REGISTRY = MODEL_DIR / "registry.json"
LOCK = threading.RLock()
MAX_MODELS = 12
PARAM_LIMITS = {"C": (0.001, 100), "max_depth": (1, 12), "learning_rate": (0.005, 1), "max_iter": (10, 600),
                "n_estimators": (10, 400), "hidden": (4, 128)}

MODEL_TYPES = {
    "elo": "Elo only — no training, uses the Elo win probability directly",
    "logistic": "Logistic regression — simple, transparent, hard to overfit",
    "gboost": "Gradient boosting — learns interactions (e.g. form matters more on clay)",
    "random_forest": "Random forest — many decision trees voting",
    "mlp": "Neural network — small multi-layer perceptron",
}
LEVEL_FILTERS = {
    "tour": ["GS", "1000", "500", "250", "WC", "DC", "OL", "UC", "ATPC", "XXI"],
    "tour_ch": ["GS", "1000", "500", "250", "WC", "DC", "OL", "UC", "ATPC", "XXI", "CH"],
}
DEFAULT_CONFIG = {
    "name": "Default logistic",
    "type": "logistic",
    "features": ["rank_log_ratio", "elo_diff", "surface_elo_diff", "form_12m_diff",
                 "surface_form_12m_diff", "last10_diff", "h2h_edge", "experience_diff"],
    "train_from": 2010, "train_to": None,  # None = year before the latest year
    "levels": "tour_ch", "min_matches": 10,
    "params": {}, "auto_retrain": True,
}


def _registry() -> dict:
    try:
        return json.loads(REGISTRY.read_text())
    except (FileNotFoundError, ValueError):
        return {"active": None, "models": {}}


def _save_registry(reg: dict):
    config.write_atomic(REGISTRY, json.dumps(reg, indent=1))


def _params(p: dict) -> dict:
    """Clamp user-supplied hyperparameters so one request can't tie up the server."""
    return {k: min(max(float(v), PARAM_LIMITS[k][0]), PARAM_LIMITS[k][1]) for k, v in (p or {}).items() if k in PARAM_LIMITS}


def _estimator(kind: str, p: dict):
    if kind == "logistic":
        return make_pipeline(StandardScaler(), LogisticRegression(C=float(p.get("C", 1.0)), max_iter=2000))
    if kind == "gboost":
        return HistGradientBoostingClassifier(max_depth=int(p.get("max_depth", 4)),
                                              learning_rate=float(p.get("learning_rate", 0.05)),
                                              max_iter=int(p.get("max_iter", 300)), random_state=0)
    if kind == "random_forest":
        return RandomForestClassifier(n_estimators=int(p.get("n_estimators", 200)),
                                      max_depth=int(p.get("max_depth", 10)),
                                      min_samples_leaf=20, n_jobs=-1, random_state=0)
    if kind == "mlp":
        h = int(p.get("hidden", 32))
        return make_pipeline(StandardScaler(), MLPClassifier(hidden_layer_sizes=(h, h // 2), alpha=1e-3,
                                                             early_stopping=True, max_iter=300, random_state=0))
    raise ValueError(f"Unknown model type {kind}")


def _filter(df, cfg):
    df = df[df.level.isin(LEVEL_FILTERS.get(cfg.get("levels"), LEVEL_FILTERS["tour_ch"]))]
    mm = int(cfg.get("min_matches") or 0)
    if mm:
        df = df[(df.p1_prior_matches >= mm) & (df.p2_prior_matches >= mm)]
    return df


def _predict(model, kind, X):
    if kind == "elo":
        return X["elo_prob"].to_numpy()
    return model.predict_proba(X)[:, 1]


def _slug(name: str) -> str:
    return re.sub(r"[^a-z0-9]+", "-", name.lower()).strip("-") or f"model-{int(time.time())}"


def train(ds: dataset.Dataset, cfg: dict, activate: bool = False, replace_only: bool = False) -> dict | None:
    """Train, score on held-out years and save. With replace_only, the result is thrown
    away if the model was deleted while it trained (used by auto-retraining)."""
    cfg = {**DEFAULT_CONFIG, **{k: v for k, v in cfg.items() if v not in (None, "")}}
    kind = cfg["type"]
    feats = ["elo_prob"] if kind == "elo" else [f for f in cfg["features"] if f in dataset.FEATURES]
    if not feats:
        raise ValueError("Pick at least one feature")
    df = _filter(ds.df, cfg)
    last_year = int(df.year.max())
    train_to = int(cfg.get("train_to") or last_year - 1)
    train_from = int(cfg["train_from"])
    tr = df[(df.year >= train_from) & (df.year <= train_to)]
    te = df[df.year > train_to]
    if len(tr) < 500 or len(te) < 100:
        raise ValueError(f"Not enough data: {len(tr)} training / {len(te)} test matches")

    model = None
    if kind != "elo":
        model = _estimator(kind, _params(cfg.get("params")))
        model.fit(tr[feats].fillna(0), tr.label)
    X_te = te[feats].fillna(0)
    p = np.clip(_predict(model, kind, X_te), 1e-6, 1 - 1e-6)
    y = te.label.to_numpy()

    # Baseline: higher-ranked player wins
    ranked = te.dropna(subset=["p1_rank", "p2_rank"])
    ranked = ranked[ranked.p1_rank != ranked.p2_rank]
    fav_acc = float(((ranked.p1_rank < ranked.p2_rank) == (ranked.label == 1)).mean())

    bins = np.linspace(0, 1, 11)
    idx = np.clip(np.digitize(p, bins) - 1, 0, 9)
    calib = [{"pred": float(p[idx == i].mean()), "actual": float(y[idx == i].mean()), "n": int((idx == i).sum())}
             for i in range(10) if (idx == i).sum() >= 20]

    by_court = {}
    for c in dataset.COURTS:
        mask = (te.court == c).to_numpy()
        if mask.sum() >= 30:
            by_court[c] = round(100 * float(((p[mask] > 0.5) == (y[mask] == 1)).mean()), 1)

    importance = {}
    if kind == "logistic":
        coef = model[-1].coef_[0]
        importance = {f: float(c) for f, c in zip(feats, coef)}
    elif kind == "random_forest":
        importance = {f: float(v) for f, v in zip(feats, model.feature_importances_)}
    elif kind in ("gboost", "mlp"):
        sample = te.sample(min(3000, len(te)), random_state=0)
        r = permutation_importance(model, sample[feats].fillna(0), sample.label, n_repeats=3,
                                   scoring="neg_log_loss", random_state=0)
        importance = {f: float(v) for f, v in zip(feats, r.importances_mean)}

    slug = _slug(cfg["name"])
    meta = {
        "slug": slug, "name": cfg["name"], "type": kind, "features": feats,
        "config": cfg, "trained_at": time.time(),
        "train_years": [train_from, train_to], "test_years": [train_to + 1, last_year],
        "n_train": int(len(tr)), "n_test": int(len(te)),
        "metrics": {
            "accuracy": round(100 * float(((p > 0.5) == (y == 1)).mean()), 2),
            "log_loss": round(float(log_loss(y, p)), 4),
            "brier": round(float(brier_score_loss(y, p)), 4),
            "auc": round(float(roc_auc_score(y, p)), 4),
            "favorite_accuracy": round(100 * fav_acc, 2),
        },
        "by_court": by_court, "calibration": calib, "importance": importance,
    }
    buf = io.BytesIO()
    joblib.dump(model, buf)
    with LOCK:
        reg = _registry()
        if replace_only and slug not in reg["models"]:
            return None
        if slug not in reg["models"] and len(reg["models"]) >= MAX_MODELS:
            raise ValueError(f"You already have {MAX_MODELS} models. Delete one first.")
        config.write_atomic(MODEL_DIR / f"{slug}.joblib", buf.getvalue())
        reg["models"][slug] = meta
        if activate or not reg.get("active"):
            reg["active"] = slug
        _save_registry(reg)
        _loaded.pop(slug, None)
    return meta


def list_models() -> dict:
    reg = _registry()
    return {"active": reg.get("active"), "models": sorted(reg["models"].values(), key=lambda m: -m["trained_at"])}


def activate(slug: str):
    with LOCK:
        reg = _registry()
        if slug not in reg["models"]:
            raise KeyError(slug)
        reg["active"] = slug
        _save_registry(reg)


def delete(slug: str):
    with LOCK:
        reg = _registry()
        reg["models"].pop(slug, None)
        if reg.get("active") == slug:
            reg["active"] = next(iter(reg["models"]), None)
        _save_registry(reg)
        (MODEL_DIR / f"{slug}.joblib").unlink(missing_ok=True)
        _loaded.pop(slug, None)


_loaded: dict = {}


def active_model():
    reg = _registry()
    slug = reg.get("active")
    if not slug or slug not in reg["models"]:
        return None, None
    if slug not in _loaded:
        _loaded[slug] = joblib.load(MODEL_DIR / f"{slug}.joblib")
    return reg["models"][slug], _loaded[slug]


def predict(ds: dataset.Dataset, a: str, b: str, court: str, level: str = "250",
            rank_a=None, rank_b=None) -> float | None:
    """P(a beats b) with the active model, or None if no model is ready."""
    meta, model = active_model()
    if meta is None or ds is None:
        return None
    f = ds.live_features(a, b, court, level, rank_a, rank_b)
    X = pd.DataFrame([f])[meta["features"]].fillna(0)
    p_ab = float(_predict(model, meta["type"], X)[0])
    # Average both orientations so the answer doesn't depend on who is "player 1"
    g = ds.live_features(b, a, court, level, rank_b, rank_a)
    p_ba = float(_predict(model, meta["type"], pd.DataFrame([g])[meta["features"]].fillna(0))[0])
    return (p_ab + (1 - p_ba)) / 2


def retrain_auto(ds: dataset.Dataset):
    """Retrain models flagged auto_retrain so they learn from the newest matches."""
    for slug in list(_registry()["models"]):
        meta = _registry()["models"].get(slug)  # re-read: the user may have deleted or switched models meanwhile
        if not meta or not meta.get("config", {}).get("auto_retrain"):
            continue
        try:
            train(ds, meta["config"], replace_only=True)
        except Exception:
            traceback.print_exc()

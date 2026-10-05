"""W4 learning: shift from rule-based weights to weights learned from real outcomes.

Each logged outcome (visited / closed = 1, lost = 0, broker overrides) stores the
feature snapshot of the deal at that moment. We fit a logistic regression on those
snapshots and blend it with the day-1 rule weights. The blend moves towards the
learned weights as data accumulates: alpha = n / (n + 250).
"""
import json

import numpy as np
from sklearn.linear_model import LogisticRegression
from sklearn.metrics import roc_auc_score
from sklearn.model_selection import train_test_split

from . import db
from .scoring import FEATURES, RULE_WEIGHTS, p_close

MIN_ROWS = 30
PRIOR_STRENGTH = 250


def _dataset(conn):
    rows = conn.execute("SELECT features, label FROM outcomes").fetchall()
    X = np.array([[json.loads(r["features"])[f] for f in FEATURES] for r in rows], dtype=float)
    y = np.array([r["label"] for r in rows], dtype=int)
    return X, y


def _auc(weights, X, y):
    preds = [p_close(weights, dict(zip(FEATURES, row))) for row in X]
    return float(roc_auc_score(y, preds))


def retrain(conn) -> dict:
    X, y = _dataset(conn)
    n = len(y)
    if n < MIN_ROWS or len(set(y)) < 2:
        model = {"kind": "rules", "weights": dict(RULE_WEIGHTS), "n_train": n, "alpha": 0.0,
                 "note": f"Need at least {MIN_ROWS} outcomes with both wins and losses to learn."}
        db.set_setting(conn, "model", model)
        return model

    X_tr, X_te, y_tr, y_te = train_test_split(X, y, test_size=0.25, random_state=7, stratify=y)
    holdout = LogisticRegression(C=1.0, max_iter=2000).fit(X_tr, y_tr)
    learned_holdout = {"intercept": float(holdout.intercept_[0]),
                       **{f: float(w) for f, w in zip(FEATURES, holdout.coef_[0])}}

    alpha = n / (n + PRIOR_STRENGTH)

    def blend(learned):
        return {k: round(alpha * learned[k] + (1 - alpha) * RULE_WEIGHTS[k], 4) for k in RULE_WEIGHTS}

    auc_rules = _auc(RULE_WEIGHTS, X_te, y_te)
    auc_blend = _auc(blend(learned_holdout), X_te, y_te)

    full = LogisticRegression(C=1.0, max_iter=2000).fit(X, y)
    learned = {"intercept": float(full.intercept_[0]), **{f: float(w) for f, w in zip(FEATURES, full.coef_[0])}}

    model = {
        "kind": "blended",
        "weights": blend(learned),
        "learned_raw": {k: round(v, 4) for k, v in learned.items()},
        "n_train": n,
        "positives": int(y.sum()),
        "alpha": round(alpha, 3),
        "auc_rules_holdout": round(auc_rules, 3),
        "auc_model_holdout": round(auc_blend, 3),
        "trained_at": db.iso(db.now()),
    }
    db.set_setting(conn, "model", model)
    return model


def model_info(conn) -> dict:
    model = db.get_setting(conn, "model")
    if not model:
        model = {"kind": "rules", "weights": dict(RULE_WEIGHTS), "n_train": 0, "alpha": 0.0}
    model["rule_weights"] = RULE_WEIGHTS
    model["broker_outcomes"] = conn.execute("SELECT COUNT(*) FROM outcomes WHERE kind = 'broker'").fetchone()[0]
    return model

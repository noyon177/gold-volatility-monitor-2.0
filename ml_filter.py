"""Runtime side of the ML filter: load the trained bundle and score a live signal."""

from ml_features import build_features


def load_bundle(path):
    import joblib
    bundle = joblib.load(path)
    for key in ("model", "features", "threshold"):
        if key not in bundle:
            raise RuntimeError(f"model file is missing '{key}'")
    try:
        import sklearn
        trained = bundle.get("sklearn_version")
        if trained and trained != sklearn.__version__:
            print(f"[ML] warning: model trained with scikit-learn {trained}, "
                  f"running {sklearn.__version__}. Pin the same version.")
    except Exception:
        pass
    return bundle


def predict_prob(bundle, plan, data5, name):
    import numpy as np
    feats = build_features(plan, data5, name)
    x = np.array([[feats[c] for c in bundle["features"]]], dtype=float)
    return float(bundle["model"].predict_proba(x)[0, 1])

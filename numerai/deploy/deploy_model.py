import logging
import numpy as np
import pandas as pd
import scipy
from tqdm import tqdm

logger = logging.getLogger(__name__)


class MultiTargetNeutralModel:
    def __init__(
        self,
        models,
        neutralisation_cols="all",
        neutralisation_prop=0.5,
        ensembling_fn=None,
    ):
        self.models = models
        self.features = next(iter(models.values())).feature_name_
        self.neutralisation_cols = neutralisation_cols
        self.neutralisation_prop = neutralisation_prop
        self.ensembling_fn = ensembling_fn or np.mean

    def predict(self, df: pd.DataFrame):
        return predict_ensemble(
            df=df,
            models=self.models,
            features=self.features,
            ensembling_fn=self.ensembling_fn,
            neutralisation_cols=self.neutralisation_cols,
            neutralisation_prop=self.neutralisation_prop,
        )


def predict_ensemble(
    df,
    models,
    features,
    ensembling_fn=np.mean,
    neutralisation_cols=None,
    neutralisation_prop=0.5,
):
    pred_cols = []
    df = df.copy()
    for model_nm, model in tqdm(
        models.items(), desc="Predicting for each model", total=len(models)
    ):
        pred_col = "pred_" + model_nm
        df[pred_col] = model.predict(df[features])
        pred_cols.append(pred_col)
    ensemble_col = "pred_ensemble"
    logger.info(f"Ensembling predictions with {ensembling_fn}: {pred_cols}")
    df[ensemble_col] = ensembling_fn(df[pred_cols], axis=1)
    if neutralisation_cols is None:
        return df[ensemble_col].values
    ntr_cols = features if neutralisation_cols == "all" else neutralisation_cols
    logger.info("Neutralising the predictions and taking the rank percent")
    return (
        neutralize(
            df=df,
            columns=[ensemble_col],
            neutralizers=ntr_cols,
            proportion=neutralisation_prop,
            normalize=True,
            era_col="era",
            verbose=True,
        )
        .rank(pct=True)
        .rename(columns={ensemble_col: "prediction"})
    )


def neutralize(
    df,
    columns,
    neutralizers=None,
    proportion=1.0,
    normalize=True,
    era_col="era",
    verbose=False,
):
    if neutralizers is None:
        neutralizers = []
    unique_eras = df[era_col].unique()
    computed = []
    if verbose:
        iterator = tqdm(unique_eras)
    else:
        iterator = unique_eras
    for u in iterator:
        df_era = df[df[era_col] == u]
        scores = df_era[columns].values.astype(np.float32)
        if normalize:
            scores2 = []
            for x in scores.T:
                x = (scipy.stats.rankdata(x, method="ordinal") - 0.5) / len(x)
                x = scipy.stats.norm.ppf(x)
                scores2.append(x)
            scores = np.array(scores2).T.astype(np.float32)
        exposures = df_era[neutralizers].values.astype(np.float32)
        scores -= proportion * exposures.dot(
            np.linalg.pinv(exposures, rcond=1e-6).dot(scores)
        )
        scores /= scores.std(ddof=0)
        computed.append(scores)
    return pd.DataFrame(np.concatenate(computed), columns=columns, index=df.index)
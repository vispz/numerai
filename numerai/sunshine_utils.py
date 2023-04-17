import functools
import json
import os.path

import pandas as pd
import dask
import numpy as np

import numerapi
import utils


from typing import Optional

DF = pd.DataFrame


def download_data(dataset_name: str, data_path: str, as_int8: bool):
    """Downloads training data

    :dataset_name: Version of the data (ex: `'v4.1'`).
    :param data_path: where data is to be saved.
    :param as_int8: If set to true, we will download files in int8 format.
         if you remove the int8 suffix for each of these files, you'll
         get features between 0 and 1 as floats. int_8 files are much smaller...
         but are harder to work with because some packages don't like ints and the way NAs are encoded.
    """
    # A mapping from to an easy name to the downloaded fl path
    downloaded_fl_map = {}
    napi = numerapi.NumerAPI()
    current_round = napi.get_current_round()
    print(f"Current round: {current_round}")
    # Tournament data changes every week so we specify the round in their name. Training
    # and validation data only change periodically, so no need to download them every time.
    print("Downloading dataset files...")
    dtype_suf = "_int8" if as_int8 else ""
    for fl_key, fl in [
        ("train", f"train{dtype_suf}.parquet"),
        ("test", f"validation{dtype_suf}.parquet"),
        ("features_json", "features.json"),
        ("val_example", "validation_example_preds.parquet"),
    ]:
        src = os.path.join(dataset_name, fl)
        dst = os.path.join(data_path, fl)
        print(f"Downloading {src} to {dst}...")
        napi.download_dataset(filename=src, dest_path=dst)
        downloaded_fl_map[fl_key] = dst
    live_src = f"{dataset_name}/live{dtype_suf}.parquet"
    live_dst = os.path.join(data_path, f"{current_round}/live{dtype_suf}.parquet")
    print(f"Downloading {live_src} to {live_dst}...")
    napi.download_dataset(filename=live_src, dest_path=live_dst)
    downloaded_fl_map["live"] = live_dst
    return downloaded_fl_map


def build_cols_to_read(
    feature_json_fl: str,
    feature_set_name: Optional[str] = None,
) -> list[str]:
    """Builds a list of columns to read from the downloaded data."""
    # read the feature metadata and get a feature set (or all the features)
    with open(feature_json_fl, "r") as f:
        feature_metadata = json.load(f)
    # features = feature_metadata["feature_sets"]["small"] # get the small feature set
    features = feature_metadata["feature_sets"][
        feature_set_name
    ]  # get the medium feature set
    target_cols = feature_metadata["targets"]
    # read in just those features along with era and target columns
    return features + target_cols + [utils.ERA_COL, utils.DATA_TYPE_COL]


def load_downloaded_data(
    downloaded_fl_map: dict[str, str],
    cols_to_read: list[str],
    val_fracs: list[float],
    to_train: bool = True,
    sample_every_nth_era: int = 1,
) -> dict[str, DF]:
    """
    :param downloaded_fl_map: as returned by :func:`download_data`.
    :param cols_to_read: as returned by :func:`build_cols_to_read`
    :param to_train: if set to false, we only load live data for prediction.
    :param sample_every_nth_era: if set to a number > 1, we subsample every nth era
        both in train and test. We don't sample live data.

    Sample input::

        to_train, val_fracs = True, [0.2, 0.2]

    Sample output::
        # First 60% of training eras will be in `train`
        # A random mix of the remaining 40% will be in val1 and val2
        # `test` is the validation dataset provided by numerai.
        {"train": DF, "val1": DF, "val2": DF, "test": DF, "live": DF}
    """
    print("Reading live data ...")
    read_parquet = functools.partial(pd.read_parquet, columns=cols_to_read)
    live_data = read_parquet(downloaded_fl_map["live"])
    if not to_train:
        print("Not loading training data")
        return {"live": live_data}
    # We want to load and split the training data
    print("Reading training data ...")
    train_full_data = _subsample_every_nth_era(
        df=read_parquet(downloaded_fl_map["train"]),
        n=sample_every_nth_era,
    )
    print("Reading test data ...")
    test_data = _subsample_every_nth_era(
        df=read_parquet(downloaded_fl_map["test"]),
        n=sample_every_nth_era,
    )
    splits = _split_train_data(train_full_df=train_full_data, val_fracs=val_fracs)
    return {**splits, "test": test_data, "live": live_data}


def _subsample_every_nth_era(df: DF, n: int = 4) -> DF:
    if n == 1:
        return df
    every_nth_era = set(df[utils.ERA_COL].unique()[::n])  # type: ignore
    return df[df[utils.ERA_COL].isin(every_nth_era)]  # type: ignore


def _split_train_data(train_full_df: DF, val_fracs: list[float]) -> dict[str, DF]:
    """We want to split the training data such that the first n eras are used as training
    and for validation, we randomly sample eras into each validation bin.

    Sample input:: val_fracs = [0.2, 0.2]
    Sample output:: {"train": DF, "val1": DF, "val2": DF}
    """
    print("Splitting into train and validations")
    eras = train_full_df[utils.ERA_COL].astype(int)
    num_eras = int(eras.max())
    # Get the first eras as training eras
    train_max_era = int((1.0 - sum(val_fracs)) * num_eras)
    # For the validation eras, randomly bucket them into each validation bin
    val_eras = list(range(train_max_era + 1, num_eras + 1))
    np.random.shuffle(val_eras)
    val_fracs = np.array(val_fracs)  # type: ignore
    val_era_bins = np.split(
        val_eras,
        # We divide the val_fracs by the sum, to make it sum to one
        # (we are only looking at val_eras now). And then we convert it into index
        # bins by multiplying by the num of val_eras and then covnerting to int.
        # np.split, will produce n+1 splits where n is the number of indices passed
        # if we include the last element, then we will create an extra empty validation df.
        indices_or_sections=(
            (val_fracs / np.sum(val_fracs)).cumsum() * len(val_eras)
        ).astype(int)[:-1],
    )
    splits = {"train": train_full_df[eras <= train_max_era]}
    for val_num, val_era_bin in enumerate(val_era_bins, start=1):
        split_nm = "val" if len(val_fracs) == 1 else f"val{val_num}"
        splits[split_nm] = train_full_df[eras.isin(set(val_era_bin))]
    return splits
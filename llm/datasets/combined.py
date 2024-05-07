import numpy as np
from datasets import concatenate_datasets

from .registry import get_dataset, list_datasets, register_dataset, get_dataset_attrs
from ..random import FixedSeed
from .llm_data_utils import LMText, LabeledStringDataCollator


def get_all_datasets_list(dataset_str, prompt_style=None):
    dataset, sub_dataset = dataset_str.split(":", 1)

    assert dataset in [
        "all",
        "eval",
    ], f"Format strings as <all|eval>:<split>, found {dataset_str}"

    all_datasets_list = []

    if dataset == "all":
        if sub_dataset == "train":
            all_datasets_list += sorted(
                list(
                    filter(
                        lambda x: not any(
                            s in x
                            for s in [
                                "all",
                                "sub",
                                "mmlu",
                                "bbmc",
                                "gsm8k",
                                "offline",
                                "modiste",
                            ]
                        ),
                        list_datasets(),
                    )
                )
            )
            ## Skip datasets for oe.
            if prompt_style == "oe":
                all_datasets_list = list(
                    filter(
                        lambda x: not any(s in x for s in ["hellaswag"]),
                        all_datasets_list,
                    )
                )
        else:
            raise NotImplementedError
    elif dataset == "eval":
        if sub_dataset == "all":
            all_datasets_list = [
                f"mmlu:{task}" for task in get_dataset_attrs("mmlu").get("tasks")
            ] + ["gsm8k"]
        elif sub_dataset == "mmlu":
            all_datasets_list = [
                f"{sub_dataset}/{task}"
                for task in get_dataset_attrs(sub_dataset).get("tasks")
            ]
        elif sub_dataset.startswith("mmlu_offline:"):
            sub_dataset, name = sub_dataset.split(":")
            all_datasets_list = [
                f"{sub_dataset}:{name}:{task}"
                for task in get_dataset_attrs(sub_dataset).get("tasks")
            ]
        elif sub_dataset == "modiste":
            all_datasets_list = [
                f"{sub_dataset}:{task}"
                for task in get_dataset_attrs(sub_dataset).get("tasks")
            ]
        else:
            raise NotImplementedError

    return all_datasets_list


def get_combined_dataset(
    all_dataset_names,
    max_n=100,
    seed=None,
    complement=False,
    uniform=False,
    **kwargs,
):
    all_train_data, all_val_data, all_test_data = [], [], []
    for dataset in all_dataset_names:
        train_data, val_data, test_data = get_dataset(
            dataset,
            seed=seed,
            **kwargs,
        )

        [
            l.append(v) if v is not None else None
            for l, v in zip(
                (all_train_data, all_val_data, all_test_data),
                (train_data, val_data, test_data),
            )
        ]

    def _concat_datasets(datasets, comp=False, unf=False):
        all_n = [len(ds) for ds in datasets]
        total_n = min(max_n, sum(all_n))

        if unf:
            equal_n = max_n // len(all_n)
            select_n = [min(equal_n, len(ds)) for ds in datasets]

            if comp:
                return concatenate_datasets(
                    [
                        ds.select(range(n, N))
                        for ds, N, n in zip(datasets, all_n, select_n)
                        if n < N
                    ]
                )

            return concatenate_datasets(
                [ds.select(range(n)) for ds, n in zip(datasets, select_n)]
            )

        select_n = ((np.array(all_n) / sum(all_n)) * total_n).astype(int)

        return concatenate_datasets(
            [
                ds.select(range(n, N) if comp else range(n))
                for ds, N, n in zip(datasets, all_n, select_n)
            ]
        )

    all_train_data = _concat_datasets(all_train_data, comp=complement, unf=uniform)
    all_val_data = _concat_datasets(all_val_data)
    all_test_data = _concat_datasets(all_test_data)

    return all_train_data, all_val_data, all_test_data


def get_all(
    *args,
    max_n=200_000,
    max_val_n=None,
    max_token_length=None,
    prompt_style=None,
    seed=137,
    num_workers=8,
    tokenizer=None,
    complement=False,
    **kwargs,
):
    dataset_names = get_all_datasets_list("all:train", prompt_style=prompt_style)

    tr, vl, _ = get_combined_dataset(
        all_dataset_names=dataset_names,
        *args,
        **kwargs,
        prompt_style=prompt_style,
        seed=seed,
        num_workers=num_workers,
        max_n=max_n,
        complement=complement,
    )

    with FixedSeed(seed):
        max_val_n = max_val_n or max_n
        vl = vl.select(
            np.random.choice(
                range(min(len(vl), max_val_n)), min(len(vl), max_val_n), replace=False
            )
        )

    if max_token_length is not None:
        tokenizer_args = LabeledStringDataCollator.get_tokenizer_args(tokenizer)

        def token_length_filter(instance):
            inputs = tokenizer(
                [str(LMText.from_(instance))],
                **tokenizer_args,
            )
            return inputs.get("input_ids").size(-1) <= max_token_length

        tr = tr.filter(token_length_filter, num_proc=num_workers)
        vl = vl.filter(token_length_filter, num_proc=num_workers)

    return tr, vl, None


@register_dataset
def all_20k_uniform(
    *args, max_n=20_000, max_val_n=2_000, max_token_length=None, **kwargs
):
    return get_all(
        *args,
        max_n=max_n,
        max_val_n=max_val_n,
        uniform=True,
        max_token_length=max_token_length,
        **kwargs,
    )


@register_dataset
def all_20k_uniform_h(*args, **kwargs):
    return all_20k_uniform(*args, with_query_label=True, **kwargs)


def all_200k_c(*args, max_n=200_000, prompt_style="choice", **kwargs):
    tr, _, _ = get_combined_dataset(
        all_dataset_names=get_all_datasets_list("all:train", prompt_style=prompt_style),
        *args,
        **kwargs,
        prompt_style=prompt_style,
        max_n=max_n,
        complement=True,
    )
    return tr, None, None


def sub_200k(
    *args, seed=None, max_n=200_000, max_val_n=2_000, prompt_style="choice", **kwargs
):
    all_dataset_names = get_all_datasets_list("all:train", prompt_style=prompt_style)
    all_dataset_names = all_dataset_names[: len(all_dataset_names) // 2]
    tr, vl, _ = get_combined_dataset(
        all_dataset_names=all_dataset_names,
        *args,
        **kwargs,
        prompt_style=prompt_style,
        max_n=max_n,
        complement=False,
    )

    with FixedSeed(seed):
        max_val_n = max_val_n or max_n
        vl = vl.select(
            np.random.choice(
                range(min(len(vl), max_val_n)), min(len(vl), max_val_n), replace=False
            )
        )

    return tr, vl, None


def cal_sub_200k(*args, max_n=200_000, prompt_style="choice", **kwargs):
    all_dataset_names = get_all_datasets_list("all:train", prompt_style=prompt_style)
    all_dataset_names = all_dataset_names[: len(all_dataset_names) // 2]
    _, vl, _ = get_combined_dataset(
        all_dataset_names=all_dataset_names,
        *args,
        **kwargs,
        prompt_style=prompt_style,
        max_n=max_n,
        complement=False,
    )
    return vl, None, None


def sub_200k_c(*args, max_n=800_000, prompt_style="choice", **kwargs):
    all_dataset_names = get_all_datasets_list("all:train", prompt_style=prompt_style)
    all_dataset_names = all_dataset_names[len(all_dataset_names) // 2 :]

    tr, _, _ = get_combined_dataset(
        all_dataset_names=all_dataset_names,
        *args,
        **kwargs,
        prompt_style=prompt_style,
        max_n=max_n,
    )
    return tr, None, None


def cal_sub_200k_c(*args, max_n=800_000, prompt_style="choice", **kwargs):
    all_dataset_names = get_all_datasets_list("all:train", prompt_style=prompt_style)
    all_dataset_names = all_dataset_names[len(all_dataset_names) // 2 :]
    _, vl, _ = get_combined_dataset(
        all_dataset_names=all_dataset_names,
        *args,
        **kwargs,
        prompt_style=prompt_style,
        max_n=max_n,
    )
    return vl, None, None

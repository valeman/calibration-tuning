from .registry import register_dataset, get_dataset, get_dataset_attrs, list_datasets
from .utils import IndexedDataset, LabelNoiseDataset, get_loader, get_num_workers

__all__ = [
    "register_dataset",
    "get_dataset",
    "get_dataset_attrs",
    "list_datasets",
    "IndexedDataset",
    "LabelNoiseDataset",
    "get_loader",
    "get_num_workers",
]


def __setup():
    from importlib import import_module

    for n in [
        "alpaca",
        "arc",
        "boolq",
        "commonsense_qa",
        "cosmos_qa",
        "hellaswag",
        "math_qa",
        "mmlu",
        "nli",
        "obqa",
        "piqa",
        "sciq",
        "story_cloze",
        "super_glue",
        "trec",
        "truthful_qa",
        "winogrande",
        "wsc",
    ]:
        import_module(f".{n}", __name__)


__setup()

import os
from dataclasses import dataclass
import torch
from transformers.trainer import Trainer, logger, TRAINING_ARGS_NAME, TrainingArguments

from ..datasets import DictCollator, LabeledStringDataCollator


class FineTuner(Trainer):
    @dataclass
    class Args(TrainingArguments): ...

    def __init__(self, *args, **kwargs):
        super().__init__(
            *args,
            **kwargs,
            data_collator=DictCollator(),
        )

        self._collate_fn = LabeledStringDataCollator(self.tokenizer)

    def compute_loss(self, model, inputs, **kwargs):
        inputs = [dict(zip(inputs.keys(), vals)) for vals in zip(*inputs.values())]
        targets = [inp.pop("target") for inp in inputs]

        loss_inputs = {
            k: v.to(self.accelerator.device)
            for k, v in self._collate_fn(
                [{**inp, "target": t} for inp, t in zip(inputs, targets)]
            ).items()
        }

        return super().compute_loss(model, loss_inputs, **kwargs)

    ## Skip eval.
    def evaluate(self, *_, **__):
        # metrics = super().evaluate(eval_dataset, ignore_keys, metric_key_prefix)
        metrics = {}
        return metrics

    def _save(self, output_dir=None, state_dict=None):
        output_dir = output_dir if output_dir is not None else self.args.output_dir
        os.makedirs(output_dir, exist_ok=True)
        logger.info(f"Saving model checkpoint to {output_dir}")

        self.model.save_pretrained(
            output_dir,
            state_dict=state_dict,
            safe_serialization=self.args.save_safetensors,
            selected_adapters=["default"],
            save_embedding_layers=False,
        )

        if self.tokenizer is not None:
            self.tokenizer.save_pretrained(output_dir)

        torch.save(self.args, os.path.join(output_dir, TRAINING_ARGS_NAME))

import logging
from tqdm.auto import tqdm
import torch
from peft import PeftModel
from sklearn.metrics import roc_auc_score

from ..datasets import (
    LabeledStringDataCollator,
    get_token_vec,
    prepare_uncertainty_query,
)
from .third_party.calibration import calibration


@torch.inference_mode()
def evaluate_via_eos(
    accelerator,
    model,
    tokenizer,
    loader,
    query_format="roman_choice",
    **_,
):
    collate_fn = LabeledStringDataCollator(tokenizer)

    all_labels, all_logits = [], []
    all_q_labels, all_q_logits = [], []

    for inputs in tqdm(loader):
        inputs = [dict(zip(inputs.keys(), vals)) for vals in zip(*inputs.values())]
        targets = [inp.pop("target") for inp in inputs]

        generation_inputs = {
            k: v.to(accelerator.device) for k, v in collate_fn(inputs).items()
        }

        if isinstance(model, PeftModel):
            model.set_adapter("default")

        generation_outputs = model(**generation_inputs)
        logits = generation_outputs.logits[:, -1, :]

        labels = torch.tensor(tokenizer(targets).get("input_ids"))[:, 1].to(
            accelerator.device
        )
        preds = logits.argmax(dim=-1)

        generations = tokenizer.batch_decode(
            preds,
            skip_special_tokens=True,
            clean_up_tokenization_spaces=False,
        )

        q_inputs, q_labels, q_token_vec = prepare_uncertainty_query(
            tokenizer,
            inputs,
            targets,
            generations,
            strategy="substring",
            format=query_format,
        )
        q_labels = q_labels.to(accelerator.device)

        q_generation_inputs = {
            k: v.to(accelerator.device) for k, v in collate_fn(q_inputs).items()
        }

        if isinstance(model, PeftModel) and "query" in model.peft_config:
            model.set_adapter("query")

        q_generation_outputs = model(**q_generation_inputs)

        q_logits = q_generation_outputs.logits[..., -1, :]

        [
            l.append(v)
            for l, v in zip(
                (all_labels, all_logits, all_q_labels, all_q_logits),
                accelerator.gather_for_metrics((labels, logits, q_labels, q_logits)),
            )
        ]

    all_labels = torch.cat(all_labels, dim=0)
    all_p = torch.cat(all_logits, dim=0).softmax(dim=-1)
    all_q_labels = torch.cat(all_q_labels, dim=0)
    all_q_p = torch.cat(all_q_logits, dim=0)[:, q_token_vec].softmax(dim=-1)

    all_pred = all_p.argmax(dim=-1)
    acc = (all_pred == all_labels).float().mean(dim=0)

    logits_ece, _ = calibration(
        all_labels,
        all_pred,
        all_p[torch.arange(all_p.size(0)), all_pred],
    )

    all_q_pred = all_q_p.argmax(dim=-1)
    q_acc = (all_q_pred == all_q_labels).float().mean(dim=0)

    q_ece, _ = calibration(
        all_q_labels,
        all_q_pred,
        all_q_p[torch.arange(all_q_p.size(0)), all_q_pred],
    )

    try:
        q_auroc = roc_auc_score(
            all_q_labels.cpu(),
            all_q_p[torch.arange(all_q_p.size(0)), all_q_pred].cpu(),
        )
    except ValueError:
        logging.warning(f"AUROC calculation failed.")
        q_auroc = float("nan")

    return {
        "N": all_q_labels.size(0),
        "logits_ece": logits_ece,
        "acc": acc.item(),
        "unc_acc": q_acc.item(),
        "unc_auroc": q_auroc,
        "unc_ece": q_ece,
    }


@torch.inference_mode()
def evaluate_contextual_calibration_via_eos(
    accelerator,
    model,
    tokenizer,
    loader,
    **_,
):
    collate_fn = LabeledStringDataCollator(tokenizer)

    all_labels, all_logits = [], []

    for inputs in tqdm(loader):
        inputs = [dict(zip(inputs.keys(), vals)) for vals in zip(*inputs.values())]
        targets = [inp.pop("target") for inp in inputs]

        platt_logits = []

        for cf_str in [
            "Question: N/A",
            "Question: ",
            f"Question: {tokenizer.pad_token}",
        ]:
            nc_inputs = [{**inp, "context": cf_str, "prompt": ""} for inp in inputs]

            nc_generation_inputs = {
                k: v.to(accelerator.device) for k, v in collate_fn(nc_inputs).items()
            }

            nc_generation_outputs = model(**nc_generation_inputs)
            nc_logits = nc_generation_outputs.logits[:, -1, :]

            platt_logits.append(nc_logits)

        platt_logits = torch.stack(platt_logits).mean(dim=0)

        generation_inputs = {
            k: v.to(accelerator.device) for k, v in collate_fn(inputs).items()
        }

        if isinstance(model, PeftModel):
            model.set_adapter("default")

        generation_outputs = model(**generation_inputs)
        logits = generation_outputs.logits[:, -1, :] - platt_logits

        labels = torch.tensor(tokenizer(targets).get("input_ids"))[:, 1].to(
            accelerator.device
        )

        [
            l.append(v)
            for l, v in zip(
                (all_labels, all_logits),
                accelerator.gather_for_metrics((labels, logits)),
            )
        ]

    all_labels = torch.cat(all_labels, dim=0)
    all_p = torch.cat(all_logits, dim=0).softmax(dim=-1)

    all_pred = all_p.argmax(dim=-1)
    acc = (all_pred == all_labels).float().mean(dim=0)

    logits_ece, _ = calibration(
        all_labels,
        all_pred,
        all_p[torch.arange(all_p.size(0)), all_pred],
    )

    return {
        "N": all_labels.size(0),
        "acc": acc.item(),
        "logits_ece": logits_ece,
    }


@torch.inference_mode()
def evaluate_candidate_via_eos(
    accelerator,
    model,
    tokenizer,
    loader,
    **_,
):
    collate_fn = LabeledStringDataCollator(tokenizer)

    cand_token_vec = get_token_vec(tokenizer, format="mcq").to(accelerator.device)

    all_labels, all_logits = [], []

    for inputs in tqdm(loader):
        inputs = [dict(zip(inputs.keys(), vals)) for vals in zip(*inputs.values())]
        targets = [inp.pop("target") for inp in inputs]

        generation_inputs = {
            k: v.to(accelerator.device) for k, v in collate_fn(inputs).items()
        }

        if isinstance(model, PeftModel):
            model.set_adapter("default")

        generation_outputs = model(**generation_inputs)
        logits = generation_outputs.logits[:, -1, cand_token_vec]

        cand_labels = torch.tensor(tokenizer(targets).get("input_ids"))[:, 1].to(
            accelerator.device
        )
        labels = (cand_labels.unsqueeze(-1) == cand_token_vec).long().argmax(dim=-1)

        [
            l.append(v)
            for l, v in zip(
                (all_labels, all_logits),
                accelerator.gather_for_metrics((labels, logits)),
            )
        ]

    all_labels = torch.cat(all_labels, dim=0)
    all_p = torch.cat(all_logits, dim=0).softmax(dim=-1)

    all_pred = all_p.argmax(dim=-1)
    acc = (all_pred == all_labels).float().mean(dim=0)

    logits_ece, _ = calibration(
        all_labels,
        all_pred,
        all_p[torch.arange(all_p.size(0)), all_pred],
    )

    return {
        "N": all_labels.size(0),
        "acc": acc.item(),
        "logits_ece": logits_ece,
    }

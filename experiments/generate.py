import os
import wandb
import pandas as pd
import torch
from accelerate import Accelerator
from tqdm.auto import tqdm
from peft import PeftModel
from transformers import GenerationConfig

from llm.datasets import get_dataset, get_loader
from llm.datasets.llm_utils import (
    LMText,
    prepare_batch,
    DataCollatorForSupervisedDataset,
)
from llm.models import get_model
from llm.models.peft import get_lora_model
from llm.logging import entrypoint

from llm.datasets.llm_utils_oe import prepare_oe_calibration_query


def prepare_model(
    accelerator,
    model_name=None,
    model_dir=None,
    peft_dir=None,
    lora_rank=None,
    lora_alpha=None,
    lora_dropout=None,
):
    tokenizer = get_model(
        f"{model_name}_tokenizer",
        model_dir=model_dir,
    )

    model = get_model(
        model_name,
        device_map={"": accelerator.local_process_index},
        torch_dtype=torch.float16,
        model_dir=model_dir,
        use_cache=False,
        tokenizer=tokenizer,
        load_in_8bit=True,
    )

    model = get_lora_model(
        model,
        peft_dir=peft_dir,
        lora_rank=lora_rank,
        lora_alpha=lora_alpha,
        lora_dropout=lora_dropout,
        is_trainable=False,
        adapter_name="default",
    )

    model.eval()

    return tokenizer, model


def generate_output(
    accelerator, model, tokenizer, loader, prompt_style="oe", generation_config=None
):
    if isinstance(model, PeftModel):
        model.set_adapter("default")

    collate_fn = DataCollatorForSupervisedDataset(tokenizer)

    for inputs in tqdm(loader):
        generation_inputs = prepare_batch(
            tokenizer,
            ## Skip "target" for generation.
            {k: v for k, v in inputs.items() if k != "target"},
            prompt_style=prompt_style,
        )
        generation_inputs = {
            k: v.to(accelerator.device)
            for k, v in collate_fn(generation_inputs).items()
        }

        generation_outputs = model.generate(
            **generation_inputs, generation_config=generation_config
        )
        generation_outputs = tokenizer.batch_decode(
            generation_outputs,
            skip_special_tokens=True,
            clean_up_tokenization_spaces=False,
        )

        outputs = [
            LMText(**{**dict(zip(inputs.keys(), vals)), "target": ""})
            for vals in zip(*inputs.values())
        ]
        outputs = [
            {
                **s.to_pydict(),
                "target": inputs["target"][i],
                "output": t[len(str(s)) :],
            }
            for i, (s, t) in enumerate(zip(outputs, generation_outputs))
        ]

        for k in outputs:
            print(k["context"])
            print(k["target_prompt"])
            print("\n##################\n##### OUTPUT #####\n##################\n")
            print(k["output"])
            print("\n*****************************************************************\n*****************************************************************\n")
        print(1/0)

        yield from outputs


def generate_outputs_main(
    seed=137,
    log_dir=None,
    dataset=None,
    data_dir=None,
    num_workers=8,
    batch_size=1,
    model_name=None,
    model_dir=None,
    peft_dir=None,
    lora_rank=8,
    lora_alpha=32,
    lora_dropout=0.1,
    use_dataset_cache=True,
    prompt_style="oe",
    max_new_tokens=30,
):
    accelerator = Accelerator()

    config = {
        "seed": seed,
        "model_name": model_name,
        "model_dir": model_dir,
        "peft_dir": peft_dir,
        "lora_rank": lora_rank,
        "lora_alpha": lora_alpha,
        "lora_dropout": lora_dropout,
        "prompt_style": prompt_style,
        "max_new_tokens": max_new_tokens,
    }
    if accelerator.is_main_process:
        wandb.config.update(config)

    tokenizer, model = prepare_model(
        accelerator,
        model_name=model_name,
        model_dir=model_dir,
        peft_dir=peft_dir,
        lora_rank=lora_rank,
        lora_alpha=lora_alpha,
        lora_dropout=lora_dropout,
    )

    with accelerator.main_process_first():
        train_data, _, _ = get_dataset(
            dataset,
            root=data_dir,
            tokenizer=tokenizer,
            seed=seed,
            num_workers=num_workers,
            use_cache=use_dataset_cache,
            prompt_style=prompt_style,
        )

    generation_config = GenerationConfig(
        pad_token_id=tokenizer.pad_token_id,
        bos_token_id=tokenizer.bos_token_id,
        eos_token_id=tokenizer.eos_token_id,
        max_new_tokens=max_new_tokens,
    )

    print(len(train_data))
    print(1/0)

    for data, split in zip([train_data], ["train"]):
        loader = get_loader(
            data,
            batch_size=batch_size,
            pin_memory=True,
            accelerator=accelerator,
            collate_fn=lambda x: {k: [d[k] for d in x] for k in x[0].keys()},
        )

        output_generator = generate_output(
            accelerator,
            model,
            tokenizer,
            loader,
            prompt_style=prompt_style,
            generation_config=generation_config,
        )

        csv_path = f"{log_dir}/outputs/{split}"
        with accelerator.main_process_first():
            if accelerator.is_main_process:
                os.makedirs(csv_path)

        pd.DataFrame(output_generator).to_csv(
            f"{csv_path}/{accelerator.process_index}.csv", index=False
        )


def generate_query_label(
    accelerator,
    model,
    tokenizer,
    loader,
    query_format="roman_choice",
    comparison_strategy="substring",
):
    if isinstance(model, PeftModel):
        model.set_adapter("default")

    for inputs in tqdm(loader):
        inputs_list = []
        for i in range(len(inputs[next(iter(inputs))])):
            new_dict = {key: value[i] for key, value in inputs.items()}
            inputs_list.append(new_dict)

        question_strings = []
        for x in inputs_list:
            x.pop("target")
            question_strings.append(str(LMText.from_(x)))

        output_strings = inputs["output"]
        oe_target_strings = inputs["target"]
        # Find the rightmost occurrence of the eos token.
        for i, x in enumerate(oe_target_strings):
            index = x.rfind(tokenizer.eos_token)
            if index != -1:
                # Everything before the substring + everything after the substring
                oe_target_strings[i] = x[:index]

        _, _, acc = prepare_oe_calibration_query(
            tokenizer,
            oe_target_strings,
            output_strings,
            question_strings,
            format=query_format,
            comparison_strategy=comparison_strategy,
        )

        outputs = [
            LMText(**{**dict(zip(inputs.keys(), vals)), "target": ""})
            for vals in zip(*inputs.values())
        ]
        outputs = [
            {
                **s.to_pydict(),
                "target": inputs["target"][i],
                "label": t.item(),
            }
            for i, (s, t) in enumerate(zip(outputs, acc))
        ]

        yield from outputs


def generate_labels_main(
    seed=137,
    log_dir=None,
    dataset=None,
    data_dir=None,
    num_workers=8,
    batch_size=1,
    model_name=None,
    model_dir=None,
    peft_dir=None,
    lora_rank=8,
    lora_alpha=32,
    lora_dropout=0.1,
    use_dataset_cache=True,
):
    accelerator = Accelerator()

    config = {
        "seed": seed,
        "model_name": model_name,
        "model_dir": model_dir,
        "peft_dir": peft_dir,
        "lora_rank": lora_rank,
        "lora_alpha": lora_alpha,
        "lora_dropout": lora_dropout,
    }
    if accelerator.is_main_process:
        wandb.config.update(config)

    tokenizer, model = prepare_model(
        accelerator,
        model_name=model_name,
        model_dir=model_dir,
        peft_dir=peft_dir,
        lora_rank=lora_rank,
        lora_alpha=lora_alpha,
        lora_dropout=lora_dropout,
    )

    with accelerator.main_process_first():
        train_data, _, _ = get_dataset(
            dataset,
            root=data_dir,
            tokenizer=tokenizer,
            seed=seed,
            num_workers=num_workers,
            use_cache=use_dataset_cache,
        )

    for data, split in zip([train_data], ["train"]):

        loader = get_loader(
            data,
            batch_size=batch_size,
            pin_memory=True,
            accelerator=accelerator,
        )

        label_generator = generate_query_label(
            accelerator,
            model,
            tokenizer,
            loader,
        )

        csv_path = f"{log_dir}/labels/{split}"
        with accelerator.main_process_first():
            if accelerator.is_main_process:
                os.makedirs(csv_path)

        pd.DataFrame(label_generator).to_csv(
            f"{csv_path}/{accelerator.process_index}.csv", index=False
        )


if __name__ == "__main__":
    import fire

    fire.Fire(
        dict(
            outputs=entrypoint(generate_outputs_main),
            labels=entrypoint(generate_labels_main),
        )
    )
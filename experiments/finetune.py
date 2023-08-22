import os
import logging
from accelerate import PartialState as AcceleratorState
from peft import LoraConfig, TaskType, get_peft_model, prepare_model_for_int8_training

from llm.logging import set_logging, wandb
from llm.datasets import get_dataset
from llm.models import get_model, get_special_tokens
from llm.utils.trainer import TrainingArguments, CalibrationTrainer


def main(
    accelerator,
    seed=137,
    log_dir=None,
    dataset=None,
    data_dir=None,
    num_workers=8,
    batch_size=1,
    grad_acc=1,
    model_name=None,
    model_dir=None,
    fp8=True,
    lora_rank=8,
    lora_alpha=32,
    lora_dropout=0.1,
    lr=1e-4,
    adam_beta2=0.999,
    unc_decay=0.0,
    unc_decay_ratio=0.0,
    unc_normalize=True,
    weight_decay=0.0,
    loss_mode="reg",
    warmup_steps=100,
    epochs=1,
):
    training_args = TrainingArguments(
        fsdp=False,
        fp16=not fp8,
        bf16=False,
        gradient_checkpointing=False,
        ddp_find_unused_parameters=False,
        num_train_epochs=epochs,
        eval_steps=1000,
        save_steps=1000,
        logging_steps=100,
        log_on_each_node=False,
        evaluation_strategy="steps",
        per_device_train_batch_size=batch_size,
        per_device_eval_batch_size=batch_size,
        loss_mode=loss_mode,
        optim="adamw_torch",
        adam_beta1=0.9,
        adam_beta2=adam_beta2,
        learning_rate=lr,
        lr_scheduler_type="cosine",
        warmup_steps=warmup_steps,
        weight_decay=weight_decay,
        unc_decay=unc_decay,
        unc_decay_ratio=unc_decay_ratio,
        unc_normalize=unc_normalize,
        gradient_accumulation_steps=grad_acc,
        output_dir=log_dir,
        report_to="wandb",
        dataloader_num_workers=4,
    )

    if accelerator.is_main_process:
        ## Manually report parameters not reported by Trainer.
        wandb.config.update(
            dict(
                seed=seed,
                log_dir=log_dir,
                dataset=dataset,
                data_dir=data_dir,
                num_workers=num_workers,
                batch_size=batch_size,
                grad_acc=grad_acc,
                model_name=model_name,
                model_dir=model_dir,
                fp8=fp8,
                lora_rank=lora_rank,
                lora_alpha=lora_alpha,
                lora_dropout=lora_dropout,
                lr=lr,
                adam_beta2=adam_beta2,
                unc_decay=unc_decay,
                unc_decay_ratio=unc_decay_ratio,
                unc_normalize=unc_normalize,
                weight_decay=weight_decay,
                loss_mode=loss_mode,
                warmup_steps=warmup_steps,
                epochs=epochs,
            )
        )

    tokenizer = get_model(
        f"{model_name}_tokenizer",
        model_dir=model_dir,
    )
    special_token_count = tokenizer.add_special_tokens(get_special_tokens(tokenizer))

    with accelerator.main_process_first():
        train_data, val_data, test_data = get_dataset(
            dataset,
            root=data_dir,
            tokenizer=tokenizer,
            seed=seed,
            num_workers=num_workers,
        )

    model = get_model(
        model_name,
        device_map={"": accelerator.local_process_index},
        load_in_8bit=fp8,
        model_dir=model_dir,
    )

    ## NOTE: Token embeddings aren't trained.
    model.resize_token_embeddings(len(tokenizer))
    if special_token_count:
        input_embeddings = model.get_input_embeddings().weight.data
        output_embeddings = model.get_output_embeddings().weight.data

        input_embeddings[-special_token_count:] = input_embeddings[
            :-special_token_count
        ].mean(dim=0, keepdim=True)
        output_embeddings[-special_token_count:] = output_embeddings[
            :-special_token_count
        ].mean(dim=0, keepdim=True)

    model = prepare_model_for_int8_training(model)
    peft_config = LoraConfig(
        task_type=TaskType.CAUSAL_LM,
        bias="none",
        r=lora_rank,
        lora_alpha=lora_alpha,
        lora_dropout=lora_dropout,
    )
    model = get_peft_model(model, peft_config)

    trainer = CalibrationTrainer(
        model=model,
        args=training_args,
        train_dataset=train_data,
        eval_dataset=val_data,
        test_dataset=test_data,
        tokenizer=tokenizer,
    )
    trainer.train()
    trainer.save_state()


def entrypoint(log_dir=None, **kwargs):
    accelerator = AcceleratorState()

    ## Only setup logging from one process.
    log_dir, finish_logging = (
        set_logging(log_dir=os.environ.get("WANDB_DIR", log_dir))
        if accelerator.is_main_process
        else [None, None]
    )
    if accelerator.is_main_process:
        logging.info(f"Working with {accelerator.num_processes} process(es).")

    main(accelerator, **kwargs, log_dir=os.environ.get("WANDB_DIR", log_dir))

    if accelerator.is_main_process:
        finish_logging()


if __name__ == "__main__":
    import fire

    fire.Fire(entrypoint)

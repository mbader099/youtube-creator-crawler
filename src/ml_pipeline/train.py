"""
Fine-tune proof: train a small causal LM on training.jsonl and log held-out loss
before vs after, so the effect of the data is measured, not asserted.

    python -m ml_pipeline.train                                  # distilgpt2, 500 records, 1 epoch
    python -m ml_pipeline.train --max-records 200 --max-length 128   # faster
    python -m ml_pipeline.train --cpu                            # force CPU (skip Apple MPS)

Outputs (in --out): the fine-tuned model + tokenizer, and metrics.json with
eval loss/perplexity before and after, the training-loss curve, sizes and runtime.
The eval split is held out from training, so "after < before" means the model
learned the distribution of the crawled text, not just memorised its training rows.
"""

import argparse
import json
import math
import os
import time
from datetime import datetime, timezone

HERE = os.path.dirname(os.path.abspath(__file__))


def parse_args(argv=None):
    ap = argparse.ArgumentParser(prog="python -m ml_pipeline.train",
                                 description="Fine-tune a small LM on training.jsonl.")
    ap.add_argument("--data", default=os.path.join(HERE, "data", "training.jsonl"))
    ap.add_argument("--model", default="distilgpt2")
    ap.add_argument("--out", default=os.path.join(HERE, "models", "distilgpt2-ft"))
    ap.add_argument("--max-records", type=int, default=500, help="sample size (CPU-friendly)")
    ap.add_argument("--eval-frac", type=float, default=0.1, help="held-out share for eval loss")
    ap.add_argument("--max-length", type=int, default=256, help="tokens per record (truncated)")
    ap.add_argument("--epochs", type=float, default=1.0)
    ap.add_argument("--batch-size", type=int, default=8)
    ap.add_argument("--lr", type=float, default=5e-5)
    ap.add_argument("--seed", type=int, default=42)
    ap.add_argument("--cpu", action="store_true", help="force CPU even if Apple MPS / CUDA is available")
    return ap.parse_args(argv)


def main(argv=None):
    a = parse_args(argv)
    if not os.path.exists(a.data):
        raise SystemExit(f"No dataset at {a.data} — run `python -m ml_pipeline run` first.")

    from datasets import load_dataset
    from transformers import (
        AutoModelForCausalLM,
        AutoTokenizer,
        DataCollatorForLanguageModeling,
        Trainer,
        TrainingArguments,
        set_seed,
    )
    set_seed(a.seed)
    t0 = time.time()

    ds = load_dataset("json", data_files=a.data, split="train").shuffle(seed=a.seed)
    ds = ds.select(range(min(a.max_records, len(ds))))
    split = ds.train_test_split(test_size=a.eval_frac, seed=a.seed)

    tok = AutoTokenizer.from_pretrained(a.model)
    tok.pad_token = tok.eos_token                     # GPT-2 family has no pad token

    def tokenize(batch):
        return tok(batch["text"], truncation=True, max_length=a.max_length)

    train_ds = split["train"].map(tokenize, batched=True, remove_columns=["text"])
    eval_ds = split["test"].map(tokenize, batched=True, remove_columns=["text"])
    n_tokens = sum(len(x) for x in train_ds["input_ids"])

    model = AutoModelForCausalLM.from_pretrained(a.model)
    model.config.pad_token_id = tok.eos_token_id

    targs = TrainingArguments(
        output_dir=os.path.join(a.out, "_trainer"), num_train_epochs=a.epochs,
        per_device_train_batch_size=a.batch_size, per_device_eval_batch_size=a.batch_size,
        learning_rate=a.lr, logging_steps=5, save_strategy="no", report_to="none",
        seed=a.seed, use_cpu=a.cpu, dataloader_pin_memory=False,
    )
    trainer = Trainer(model=model, args=targs, train_dataset=train_ds, eval_dataset=eval_ds,
                      data_collator=DataCollatorForLanguageModeling(tok, mlm=False))
    device = str(trainer.args.device)
    print(f"  model {a.model} · train {len(train_ds)} / eval {len(eval_ds)} records · "
          f"{n_tokens:,} train tokens · device {device}")

    before = trainer.evaluate()["eval_loss"]
    print(f"  eval loss BEFORE fine-tune: {before:.4f}  (perplexity {math.exp(before):,.1f})")
    train_out = trainer.train()
    after = trainer.evaluate()["eval_loss"]
    print(f"  eval loss AFTER  fine-tune: {after:.4f}  (perplexity {math.exp(after):,.1f})")

    os.makedirs(a.out, exist_ok=True)
    trainer.save_model(a.out)
    tok.save_pretrained(a.out)

    metrics = {
        "finished_at": datetime.now(timezone.utc).isoformat(timespec="seconds"),
        "base_model": a.model, "data": a.data, "device": device,
        "records_train": len(train_ds), "records_eval": len(eval_ds),
        "train_tokens": n_tokens, "max_length": a.max_length,
        "epochs": a.epochs, "batch_size": a.batch_size, "learning_rate": a.lr, "seed": a.seed,
        "eval_loss_before": round(before, 4), "eval_loss_after": round(after, 4),
        "perplexity_before": round(math.exp(before), 2), "perplexity_after": round(math.exp(after), 2),
        "eval_loss_reduction_pct": round(100 * (before - after) / before, 2),
        "train_loss": round(train_out.training_loss, 4),
        "train_loss_curve": [{"step": h["step"], "loss": round(h["loss"], 4)}
                             for h in trainer.state.log_history if "loss" in h],
        "runtime_s": round(time.time() - t0, 1),
    }
    with open(os.path.join(a.out, "metrics.json"), "w") as f:
        json.dump(metrics, f, indent=2)
    print(f"\n  ✅ held-out loss {before:.4f} → {after:.4f} "
          f"({metrics['eval_loss_reduction_pct']}% lower) · perplexity "
          f"{metrics['perplexity_before']:,} → {metrics['perplexity_after']:,} · "
          f"{metrics['runtime_s']}s")
    print(f"  model + metrics.json → {a.out}")
    return metrics


if __name__ == "__main__":
    main()

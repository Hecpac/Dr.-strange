#!/usr/bin/env python3
"""Upload training data and launch OpenAI finetuning job."""
import os, sys
sys.path.insert(0, os.path.join(os.path.dirname(__file__), '..'))

from dotenv import load_dotenv
load_dotenv(os.path.join(os.path.dirname(__file__), '..', '.env'))

from openai import OpenAI

client = OpenAI(api_key=os.environ['OPENAI_API_KEY'])

DATA_DIR = os.path.join(os.path.dirname(__file__), '..', 'data', 'finetune')

# Upload training file
print("Uploading training file...")
with open(os.path.join(DATA_DIR, 'train.jsonl'), 'rb') as f:
    train_file = client.files.create(file=f, purpose='fine-tune')
print(f"Training file ID: {train_file.id}")

# Upload validation file
print("Uploading validation file...")
with open(os.path.join(DATA_DIR, 'val.jsonl'), 'rb') as f:
    val_file = client.files.create(file=f, purpose='fine-tune')
print(f"Validation file ID: {val_file.id}")

# Launch finetuning job
print("Launching finetuning job...")
job = client.fine_tuning.jobs.create(
    training_file=train_file.id,
    validation_file=val_file.id,
    model="gpt-4o-2024-08-06",
    suffix="claw-hector",
    hyperparameters={
        "n_epochs": 3,
    }
)

print(f"Job ID: {job.id}")
print(f"Status: {job.status}")
print(f"Model: {job.model}")
print(f"Fine-tuned model will be: ft:gpt-4o-2024-08-06:*:claw-hector:*")

# Save job info
import json
with open(os.path.join(DATA_DIR, 'job_info.json'), 'w') as f:
    json.dump({
        'job_id': job.id,
        'train_file_id': train_file.id,
        'val_file_id': val_file.id,
        'model': job.model,
        'status': job.status,
    }, f, indent=2)
print(f"Job info saved to data/finetune/job_info.json")

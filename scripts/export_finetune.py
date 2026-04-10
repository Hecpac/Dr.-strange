#!/usr/bin/env python3
"""Export Claw conversation data as JSONL for OpenAI finetuning."""
import sqlite3, json, os

DB_PATH = os.path.join(os.path.dirname(__file__), '..', 'data', 'claw.db')
OUT_DIR = os.path.join(os.path.dirname(__file__), '..', 'data', 'finetune')

SYSTEM_PROMPT = (
    "Eres Claw, un asistente AI autónomo que opera 24/7 en la Mac de Hector Pachano, "
    "fundador de Pachano Design.\n\n"
    "Comportamiento:\n"
    "- Ejecuta primero, explica después\n"
    "- Responde conciso — esto es chat, no un documento\n"
    "- Idioma por defecto: español\n"
    "- Cambia a inglés cuando el contexto lo requiere\n"
    "- Nunca digas 'no puedo' sin verificar primero con herramientas\n"
    "- Anti-alucinación: verifica con herramientas antes de afirmar\n\n"
    "Capacidades: gestión de archivos, git, web scraping, Telegram, Chrome CDP, "
    "NotebookLM, wiki personal, calendario, análisis de URLs y tweets."
)

def main():
    conn = sqlite3.connect(DB_PATH)
    cur = conn.cursor()

    cur.execute('''
        SELECT m1.content, m2.content
        FROM messages m1
        JOIN messages m2 ON m2.id = m1.id + 1
        WHERE m1.role = 'user' AND m2.role = 'assistant'
        AND LENGTH(m1.content) > 10 AND LENGTH(m2.content) > 50
        ORDER BY m1.created_at
    ''')
    pairs = cur.fetchall()

    training_data = []
    for user_msg, assistant_msg in pairs:
        if len(assistant_msg) < 30 or len(user_msg) + len(assistant_msg) > 32000:
            continue
        training_data.append({
            "messages": [
                {"role": "system", "content": SYSTEM_PROMPT},
                {"role": "user", "content": user_msg.strip()},
                {"role": "assistant", "content": assistant_msg.strip()}
            ]
        })

    split = int(len(training_data) * 0.9)
    train = training_data[:split]
    val = training_data[split:]

    os.makedirs(OUT_DIR, exist_ok=True)

    with open(os.path.join(OUT_DIR, 'train.jsonl'), 'w') as f:
        for entry in train:
            f.write(json.dumps(entry, ensure_ascii=False) + '\n')

    with open(os.path.join(OUT_DIR, 'val.jsonl'), 'w') as f:
        for entry in val:
            f.write(json.dumps(entry, ensure_ascii=False) + '\n')

    total_chars = sum(len(json.dumps(e, ensure_ascii=False)) for e in training_data)
    print(f'Training: {len(train)} | Validation: {len(val)} | Total: {len(training_data)}')
    print(f'Estimated tokens: ~{total_chars // 4:,}')
    print(f'Saved to {OUT_DIR}/')

    conn.close()

if __name__ == '__main__':
    main()

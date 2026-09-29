"""Inference-only worker shipped to Modal. No training or private constants.

The fixed question is supplied as private model metadata. All row data consists
of anonymous ids and compact states; the Posting-key join stays on the Mac.
"""
from __future__ import annotations

import hashlib
import json
import time
from pathlib import Path
from types import SimpleNamespace


def native_state(state: dict) -> dict:
    facts, separator, policy = state['confirmed_profile'].rpartition('Policy: ')
    if not separator:
        raise ValueError('Compact policy missing')
    return {'posting': {'title': state['title'], 'employer': state['employer'],
                        'description_text': state['requirements_and_qualifications']},
            'applicant': facts.strip(), 'policy': json.loads(policy)}


def prepare(rows: list[dict], metadata: dict, directory: str, volume) -> dict:
    base = Path(metadata['base_path'])
    if not (base / 'tokenizer.json').is_file() or not (base / 'config.json').is_file():
        raise FileNotFoundError('Keep base snapshot unavailable')
    if not (Path(metadata['adapter_path']) / 'adapter_model.safetensors').is_file():
        raise FileNotFoundError('Keep adapter unavailable')
    from transformers import AutoTokenizer
    from decider.infer import Decider
    from decider.prompt import chat_for_model
    from decider.systemone import render_state

    started = time.perf_counter()
    tokenizer = AutoTokenizer.from_pretrained(base, local_files_only=True)
    config = json.loads((base / 'decider_config.json').read_text(encoding='utf-8'))
    decider = object.__new__(Decider)
    decider.m = SimpleNamespace(tok=tokenizer)
    decider.chat = chat_for_model(str(base), tokenizer)
    decider.neutralize_none = config.get('neutralize_none', True)
    decider.schema_first = False
    decider.isolated_levels = True
    target = Path(directory)
    target.mkdir(parents=True, exist_ok=True)
    native = padded = longest = context_max = 0
    with (target / 'items.jsonl').open('w', encoding='utf-8', newline='\n') as output:
        for row in rows:
            state = native_state(row['state'])
            context = len(tokenizer.encode('Context:\n' + render_state(state), add_special_tokens=False))
            _, index, items = decider._system_one_items(
                state, metadata['question'], independent=True, isolated=True,
                layout='state_first', max_state_tokens=32768,
            )
            if len(index) != 1 or len(items) != 1 or items[0]['nopts'] != [2]:
                raise ValueError('Keep question did not produce one binary item')
            length = len(items[0]['ids'])
            if context >= 32768 or length >= 32768:
                raise ValueError('Keep input exceeds the untruncated context')
            native += length
            padded += ((length + 63) // 64) * 64
            longest = max(longest, length)
            context_max = max(context_max, context)
            output.write(json.dumps({'id': row['id'], 'item': items[0]}, separators=(',', ':')) + '\n')
    digest = hashlib.sha256((target / 'items.jsonl').read_bytes()).hexdigest()
    volume.commit()
    return {'postings': len(rows), 'native_tokens': native, 'padded_tokens': padded,
            'max_row_tokens': longest, 'max_context_tokens': context_max,
            'items_sha256': digest, 'prepare_seconds': time.perf_counter() - started}


def score(metadata: dict, directory: str, items_sha256: str):
    import torch
    from decider.infer import Decider
    from decider import temperature as temperature
    from peft import PeftModel

    started = time.perf_counter()
    torch.set_num_threads(2)
    torch.manual_seed(211)
    torch.backends.cuda.matmul.allow_tf32 = True
    adapter = Path(metadata['adapter_path'])
    if hashlib.sha256((adapter / 'adapter_model.safetensors').read_bytes()).hexdigest() != metadata['adapter_sha256']:
        raise ValueError('Keep adapter identity changed')
    path = Path(directory) / 'items.jsonl'
    if hashlib.sha256(path.read_bytes()).hexdigest() != items_sha256:
        raise ValueError('Prepared keep input changed')
    decider = Decider(metadata['base_path'], device='cuda', dtype=torch.bfloat16, use_graphs=False)
    if decider.T_by_type != {'noul': 1.624, 'score': 1.124, 'choice': 1.164}:
        raise ValueError('Keep inference temperature changed')
    decider.m.requires_grad_(False)
    decider.m.lm.model = PeftModel.from_pretrained(decider.m.lm.model, str(adapter), is_trainable=False)
    decider.m.eval()
    pending = []
    with torch.no_grad(), path.open(encoding='utf-8') as source:
        for line in source:
            row = json.loads(line)
            one = [row['item']]
            probability = decider._system_one_probs(
                one, 'state_first', 32768, temperature.for_items(decider.T, decider.T_by_type, one),
            )[0][1]
            pending.append({'id': row['id'], 'probability': probability})
            if len(pending) == 25:
                yield {'scores': pending}
                pending = []
        if pending:
            yield {'scores': pending}
    torch.cuda.synchronize()
    yield {'seconds': time.perf_counter() - started}

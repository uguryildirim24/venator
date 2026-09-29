"""Private Modal worker for a two-epoch keep-only LoRA from the current adapter.

Only anonymous compact states and 0/1 labels enter this worker. Neither the
training rows nor its adapter is checked into the repository.
"""
from __future__ import annotations

import hashlib
import json
import math
import random
from pathlib import Path
from types import SimpleNamespace

from venator.score.inference import native_state


def train(payload: bytes, metadata: dict, destination: str, volume) -> dict:
    import torch
    from decider.infer import Decider
    from decider.model import collate
    from decider.prompt import chat_for_model
    from peft import PeftModel
    from transformers import AutoTokenizer

    rows = [json.loads(line) for line in payload.splitlines()]
    if len(rows) < 2 or any(set(row) != {'id', 'keep', 'state'} or row['keep'] not in (0, 1) for row in rows):
        raise ValueError('Invalid anonymous training rows')
    base = Path(metadata['base_path'])
    adapter = Path(metadata['adapter_path'])
    if hashlib.sha256((adapter / 'adapter_model.safetensors').read_bytes()).hexdigest() != metadata['adapter_sha256']:
        raise ValueError('Current adapter identity changed')
    torch.set_num_threads(2)
    torch.manual_seed(211)
    torch.backends.cuda.matmul.allow_tf32 = True
    tokenizer = AutoTokenizer.from_pretrained(base, local_files_only=True)
    config = json.loads((base / 'decider_config.json').read_text(encoding='utf-8'))
    formatter = object.__new__(Decider)
    formatter.m = SimpleNamespace(tok=tokenizer)
    formatter.chat = chat_for_model(str(base), tokenizer)
    formatter.neutralize_none = config.get('neutralize_none', True)
    formatter.schema_first = False
    formatter.isolated_levels = True
    items = []
    for row in rows:
        _, index, one = formatter._system_one_items(native_state(row['state']), metadata['question'],
            independent=True, isolated=True, layout='state_first', max_state_tokens=32768)
        if len(index) != 1 or len(one) != 1 or one[0]['nopts'] != [2] or len(one[0]['ids']) >= 32768:
            raise ValueError('Keep question or compact context changed')
        items.append(one[0])
    counts = {value: sum(row['keep'] == value for row in rows) for value in (0, 1)}
    weights = {value: len(rows) / (2 * count) if count else 0 for value, count in counts.items()}
    decider = Decider(str(base), device='cuda', dtype=torch.bfloat16, use_graphs=False)
    if decider.T_by_type != {'noul': 1.624, 'score': 1.124, 'choice': 1.164}:
        raise ValueError('Keep inference temperature changed')
    decider.m.requires_grad_(False)
    decider.m.lm.model = PeftModel.from_pretrained(decider.m.lm.model, str(adapter), is_trainable=True)
    decider.m.lm.model.gradient_checkpointing_enable(gradient_checkpointing_kwargs={'use_reentrant': False})
    decider.m.lm.model.enable_input_require_grads()
    decider.m.lm.config.use_cache = False
    parameters = [parameter for parameter in decider.m.parameters() if parameter.requires_grad]
    if not parameters or any('lora_' not in name for name, parameter in decider.m.named_parameters() if parameter.requires_grad):
        raise ValueError('Only LoRA weights may train')
    optimizer = torch.optim.AdamW(parameters, lr=3e-5, betas=(.9, .95), weight_decay=.01)
    order = []
    for epoch in range(2):
        indices = list(range(len(rows)))
        random.Random(211 + epoch).shuffle(indices)
        order.extend(indices)
    steps = math.ceil(len(order) / 8)
    decider.m.train()
    for step in range(steps):
        optimizer.zero_grad(set_to_none=True)
        position = step + 1
        lr = 3e-5 * position / 10 if position <= 10 else 3e-6 + (3e-5 - 3e-6) * (1 + math.cos(math.pi * (position - 10) / max(1, steps - 10))) / 2
        for group in optimizer.param_groups:
            group['lr'] = lr
        logical = order[step * 8:(step + 1) * 8]
        physical = []
        current = []
        for index in logical:
            proposed = current + [index]
            rounded = ((max(len(items[i]['ids']) for i in proposed) + 63) // 64) * 64
            if current and (len(proposed) > 8 or rounded * len(proposed) > 12288):
                physical.append(current)
                current = [index]
            else:
                current = proposed
        if current:
            physical.append(current)
        for batch in physical:
            tensors = collate([items[i] for i in batch], decider.m.tok.pad_token_id)
            logits = decider.m.slot_logits(*[tensors[key].cuda() for key in ('input_ids', 'attention_mask', 'slot_idx', 'slot_batch', 'nopts')])[:, :2]
            targets = torch.tensor([[1 - rows[i]['keep'], rows[i]['keep']] for i in batch], device='cuda', dtype=torch.float32)
            weight = torch.tensor([weights[rows[i]['keep']] for i in batch], device='cuda', dtype=torch.float32)
            loss = (-(targets * torch.log_softmax(logits / decider.T_by_type['noul'], dim=-1)).sum(-1) * weight).sum() / len(logical)
            if not bool(torch.isfinite(loss)):
                raise ValueError('Nonfinite keep loss')
            loss.backward()
        torch.nn.utils.clip_grad_norm_(parameters, 1., error_if_nonfinite=True)
        optimizer.step()
    target = Path(destination)
    if target.exists():
        raise FileExistsError('Candidate adapter already exists')
    decider.m.lm.model.save_pretrained(target)
    volume.commit()
    return {'adapter_path': str(target), 'adapter_sha256': hashlib.sha256((target / 'adapter_model.safetensors').read_bytes()).hexdigest()}

"""Committed identity bytes and source sensitivity."""

from __future__ import annotations

import json
from pathlib import Path

from venator.profile.loader import load_profile
from venator.discover.store import posting_revision
from venator.qualify.compile import compile_profile
from venator.qualify.prompt import SYSTEM_PROMPT
from venator.qualify.versions import (COMPILER_VERSION, DENY_VERSION, ENGINE_VERSION,
    PROMPT_VERSION, SCHEMA_VERSION, TEXT_VERSION, canon, contract_version,
    deny_version, event_id, h, input_version, profile_hash, promotion_state_revision,
    qualifier_version, schema_version)

ROOT = Path(__file__).resolve().parents[2]
FIXTURE = Path(__file__).parent / "fixtures/identities/qualification.json"
SOURCE = ROOT / "src/venator/qualify"


def test_committed_identifiers_byte_for_byte() -> None:
    golden = json.loads(FIXTURE.read_text())
    compiled = compile_profile(load_profile(ROOT / "tests/qualify/fixtures/profiles/two-degrees"),
                               as_of_month=golden["inputs"]["as_of_month"])
    inputs = golden["inputs"]
    posting = json.loads((ROOT / "tests/qualify/fixtures/postings/free.json").read_text())["posting"]
    assert posting_revision(posting) == inputs["posting_revision"]
    events = [{**row, "event_id": event_id(row)} for row in inputs["events"]]
    actual = {
        "compiler_version": COMPILER_VERSION, "engine_version": ENGINE_VERSION,
        "text_version": TEXT_VERSION, "prompt_version": PROMPT_VERSION,
        "schema_version": SCHEMA_VERSION, "deny_version": DENY_VERSION,
        "contract_version": contract_version(), "profile_hash": profile_hash(compiled),
        "posting_revision": inputs["posting_revision"],
        "input_version": input_version(inputs["posting_key"], inputs["posting_revision"],
                                       compiled.profile_hash, inputs["as_of_month"]),
        "qualifier_version": qualifier_version(**inputs["qualifier"]),
        "event_ids": [row["event_id"] for row in events],
        "promotion_state_revision": promotion_state_revision(events),
    }
    assert compiled.profile_hash == actual["profile_hash"]
    assert actual == golden["identities"]
    assert canon({"é": 1, "a": [2]}) == '{"a":[2],"é":1}'
    assert h("x") == "2d711642b726"


def test_each_source_byte_moves_its_identity() -> None:
    profile_bytes = (SOURCE / "schema.py").read_bytes()
    output_bytes = (SOURCE / "schemas/qualify-output-2.json").read_bytes()
    deny_bytes = (SOURCE / "deny_terms.py").read_bytes()
    assert h(SYSTEM_PROMPT) == PROMPT_VERSION
    assert h(SYSTEM_PROMPT + "x") != PROMPT_VERSION
    assert schema_version(profile_bytes, output_bytes) == SCHEMA_VERSION
    assert schema_version(profile_bytes + b"x", output_bytes) != SCHEMA_VERSION
    assert schema_version(profile_bytes, output_bytes + b"x") != SCHEMA_VERSION
    assert deny_version(deny_bytes) == DENY_VERSION
    assert deny_version(deny_bytes + b"x") != DENY_VERSION
    for name in ("compiler_version", "engine_version", "text_version", "prompt_version",
                 "schema_version_value", "deny_version_value"):
        assert contract_version(**{name: "changed"}) != contract_version(), name


def test_clock_reads_only_at_allowlisted_call_function() -> None:
    import ast
    import re

    def clock_sites(relative_path: str, source: str) -> list[tuple[str, str, str, bool]]:
        tree = ast.parse(source)
        found: list[tuple[str, str, str, bool]] = []
        for line_number, line in enumerate(source.splitlines(), 1):
            for match in re.finditer(r'\b(?:datetime\.now|date\.today|time\.(?:time|monotonic|perf_counter))\s*\(', line):
                functions = [node for node in ast.walk(tree)
                             if isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef))
                             and node.lineno <= line_number <= (node.end_lineno or node.lineno)]
                owner = min(functions, key=lambda node: (node.end_lineno or node.lineno) - node.lineno) if functions else None
                found.append((relative_path, owner.name if owner else '<module>',
                              match.group(0).rstrip('('), owner in tree.body if owner else False))
        return found

    def allowed(site: tuple[str, str, str, bool]) -> bool:
        relative_path, owner, _, top_level = site
        return relative_path in {
            'src/venator/qualify/pending.py', 'src/venator/qualify/export.py',
            'src/venator/qualify/record.py', 'src/venator/qualify/status.py',
        } and owner == 'main' and top_level

    sites: list[tuple[str, str, str, bool]] = []
    for path in sorted((ROOT / 'src/venator/qualify').rglob('*.py')):
        relative_path = path.relative_to(ROOT).as_posix()
        sites.extend(clock_sites(relative_path, path.read_text(encoding='utf-8')))
    assert all(allowed(site) for site in sites), [site for site in sites if not allowed(site)]

    planted = [
        ('src/venator/qualify/export.py', 'def main():\n    return date.today()\n', True),
        ('src/venator/qualify/export.py', 'def helper():\n    return date.today()\n', False),
        ('src/venator/qualify/other.py', 'def main():\n    return date.today()\n', False),
        ('src/venator/qualify/export.py', 'def outer():\n    def main():\n        return date.today()\n', False),
        ('src/venator/qualify/clock.py', 'def run():\n    return datetime.now()\n', False),
    ]
    for path, source, expected_allowed in planted:
        found = clock_sites(path, source)
        assert len(found) == 1
        assert allowed(found[0]) is expected_allowed, path


def test_identity_dict_shapes_independent_of_helpers() -> None:
    import hashlib
    def digest(value: object) -> str:
        encoded = json.dumps(value, sort_keys=True, separators=(',', ':'), ensure_ascii=False).encode()
        return hashlib.sha256(encoded).hexdigest()[:12]
    inputs = json.loads(FIXTURE.read_text())['inputs']
    qualifier = {**inputs['qualifier'], 'contract_version': contract_version(), 'precision': 'bf16',
                 'decoding': {'greedy': True, 'max_new_tokens': 3072, 'seed': 1729}}
    assert qualifier_version(**inputs['qualifier']) == digest(qualifier)
    events = [{**row, 'event_id': digest(row)} for row in inputs['events']]
    expected = hashlib.sha256('|'.join(row['event_id'] for row in events
        if row['event'] in ('activated', 'revoked', 'inherited')).encode()).hexdigest()[:12]
    assert promotion_state_revision(events) == expected

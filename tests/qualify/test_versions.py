"""Shared compact-state primitives read no wall clock."""

from __future__ import annotations

from pathlib import Path

ROOT = Path(__file__).resolve().parents[2]


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

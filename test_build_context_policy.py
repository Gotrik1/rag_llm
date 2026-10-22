"""Static release policy guards, not an emulation of Docker's ignore engine."""
from pathlib import Path

import pytest


ROOT = Path(__file__).resolve().parent


@pytest.mark.parametrize('directory', [
    '.venv312/', 'output/', 'tmp/', 'artifacts/', '.playwright-cli/',
])
def test_local_qa_and_private_diagnostics_are_explicitly_excluded_from_build_context(directory):
    rules = {line.strip() for line in (ROOT / '.dockerignore').read_text(encoding='utf-8').splitlines()
             if line.strip() and not line.lstrip().startswith('#')}
    assert directory in rules
    assert not any(rule == '!' + directory or rule.startswith('!' + directory) for rule in rules)


def test_focus_module_is_a_root_runtime_source_not_a_qa_artifact():
    assert (ROOT / 'search_focus.py').is_file()
    assert 'COPY --chown=rag:rag . .' in (ROOT / 'Dockerfile').read_text(encoding='utf-8')
    assert 'COPY . ./' in (ROOT / 'Dockerfile.backend').read_text(encoding='utf-8')

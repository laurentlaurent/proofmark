from pathlib import Path

import pytest

from proofmark import config
from proofmark.schemas import Sources

ROOT = Path(__file__).resolve().parent.parent
SAMPLES = ROOT / "samples"


@pytest.fixture(autouse=True)
def offline_env(monkeypatch, tmp_path):
    """Tests never touch a real provider or the real audit log, whatever is in .env."""
    for name in ("GEMINI_API_KEY", "GROQ_API_KEY", "LLM_API_KEY", "LLM_BASE_URL", "LLM_MODEL", "PROOFMARK_VISION",
                 "GEMINI_MODEL", "GROQ_MODEL", "OLLAMA_MODEL", "OLLAMA_BASE_URL"):
        monkeypatch.delenv(name, raising=False)
    monkeypatch.setenv("PROOFMARK_PROVIDER", "demo")
    monkeypatch.setattr(config, "AUDIT_DIR", tmp_path / "audit")
    monkeypatch.setattr(config, "AUDIT_ENABLED", True)


@pytest.fixture
def deposit_sources() -> Sources:
    folder = SAMPLES / "deposit-rescue"
    return Sources(spec=(folder / "spec.md").read_text(encoding="utf-8"), previous_spec=(folder / "previous.md").read_text(encoding="utf-8"),
                   code_diff=(folder / "changes.diff").read_text(encoding="utf-8"))


@pytest.fixture
def streak_sources() -> Sources:
    return Sources(spec=(SAMPLES / "streak-shield" / "spec.md").read_text(encoding="utf-8"))

"""Canonical runtime storage paths shared by the RAG application."""

from __future__ import annotations

import os
from pathlib import Path


PROJECT_DIR = Path(__file__).resolve().parent
DATA_DIR = Path(os.environ.get("RAG_DATA_DIR", PROJECT_DIR / ".data")).expanduser()
STATE_DIR = DATA_DIR / "state"
CACHE_DIR = DATA_DIR / "cache"
INGESTION_CACHE_DIR = CACHE_DIR / "ingestion"
MATHML_CACHE_DIR = CACHE_DIR / "mathml"
UPLOAD_DIR = DATA_DIR / "uploads"
CERTIFICATES_DIR = DATA_DIR / "certificates"
SYSTEM_PROMPT_STORE = STATE_DIR / "system_prompts"
LOG_DIR = DATA_DIR / "logs"

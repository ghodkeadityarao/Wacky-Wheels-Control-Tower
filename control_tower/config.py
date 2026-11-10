from __future__ import annotations

import os
from dataclasses import asdict, dataclass
from pathlib import Path

PKG_DIR = Path(__file__).resolve().parent.parent      # hack_the_highway_package/
DATA_DIR = PKG_DIR / "data"


@dataclass
class Settings:
    # Where a transferred order lands when its own plant has no free slot. The handbook says
    # "another plant that builds the model, on any day with a free slot"; we take the earliest such
    # (day, plant) on or after this anchor:
    #   original_date (default) | disruption_start | today | restart
    transfer_anchor: str = os.environ.get("CT_TRANSFER_ANCHOR", "original_date")
    llm_backend: str = os.environ.get("CT_LLM", "none")            # none | ollama | openai
    llm_model: str = os.environ.get("CT_LLM_MODEL", "qwen2.5:1.5b")
    llm_url: str | None = os.environ.get("CT_LLM_URL")

    def to_json(self):
        return asdict(self)

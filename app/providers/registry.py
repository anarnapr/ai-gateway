from __future__ import annotations

from pathlib import Path
from typing import Optional

import yaml

from app.providers.base import Provider
from app.providers.gemini.provider import GeminiProvider

_BUILDERS = {
    "gemini": GeminiProvider,
}


class ProviderRegistry:
    """Loads config/models.yaml once at startup and instantiates one Provider per
    configured section. Adding a new provider (e.g. anthropic) means: implement the
    Provider ABC, register its builder here, and add a section to models.yaml — no
    other code changes required. No hot-reload in v1; restart to pick up config edits.
    """

    def __init__(self, config_path: str):
        self._providers: dict[str, Provider] = {}
        self._load(config_path)

    def _load(self, config_path: str) -> None:
        path = Path(config_path)
        if not path.exists():
            raise FileNotFoundError(f"Models config not found: {config_path}")

        with open(path) as f:
            data = yaml.safe_load(f) or {}

        for provider_name, section in data.items():
            builder = _BUILDERS.get(provider_name)
            if builder is None:
                continue
            self._providers[provider_name] = builder(
                model_priority=section.get("model_priority", []),
                model_aliases=section.get("model_aliases", {}),
                quota_table=section.get("quota_table", {}),
            )

    def get(self, name: str) -> Optional[Provider]:
        return self._providers.get(name)

    def names(self) -> list[str]:
        return list(self._providers.keys())

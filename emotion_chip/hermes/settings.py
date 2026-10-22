"""Small injectable boundaries for host config, session context and plugin data."""
import os
from dataclasses import dataclass
from pathlib import Path

DEFAULTS = {
    "backend": "fish", "fish_model": "s2.1-pro", "guide_mode": "generate",
    "signals": "off", "sounds_per_turn": 1, "business_platforms": ["slack", "email"],
    "delivery_log": True,
}
FISH_MODELS = {"s2.1-pro", "s2.1-pro-free", "s2-pro", "drama-3", "s1", "drama-3-preview"}


@dataclass(frozen=True)
class FishModel:
    pinned: str | None
    requested: str | None
    effective: str


def canonical_fish_model(value):
    return ("drama-3" if value == "drama-3-preview" else value) if isinstance(value, str) and value in FISH_MODELS else None


def session_identity(env, situation):
    session_id, key = env("HERMES_SESSION_ID"), env("HERMES_SESSION_KEY")
    return session_id or situation.get("session_keys", {}).get(key) or ("key:" + key if key else "")


def sound_limit(settings, situation, env):
    if env("HERMES_SESSION_PLATFORM") in settings.get("business_platforms") or situation.get("quiet", False):
        return 0
    return settings.get("sounds_per_turn")


def read_config(key, default=None):
    try:
        from hermes_cli.config import load_config_readonly
    except ImportError:
        return default
    value = load_config_readonly() or {}
    for part in key.split("."):
        if not isinstance(value, dict) or part not in value:
            return default
        value = value[part]
    return value


def session_env(name, default=""):
    try:
        from gateway.session_context import get_session_env
    except ImportError:
        return os.environ.get(name, default)
    return get_session_env(name, default)


def plugin_data_dir():
    from hermes_constants import get_hermes_home
    return Path(get_hermes_home()) / "plugin-data" / "emotion-chip"


class Settings:
    def __init__(self, ctx=None, reader=None):
        self.ctx = ctx
        self.reader = reader or read_config

    def get(self, key):
        default = DEFAULTS[key]
        value = self.ctx.get_config(key, default) if self.ctx is not None else default
        if key == "signals":
            value = value.strip().lower() if isinstance(value, str) else "off"
            return value if value in {"facts", "facts+affordance"} else "off"
        return value

    def host(self, key, default=None):
        return self.reader(key, default)

    def tts_provider(self):
        return str(self.host("tts.provider", "edge")).strip().lower()

    def host_fish_model(self):
        return self.resolve_fish_model().pinned

    def resolve_fish_model(self, model=None, use_setting=True):
        """Fish's order: its pinned tts.fish-audio.model, then the call's model, then its wallet default.

        use_setting=False is for the compat path, where the chip's own fish_model setting never reaches Fish.
        """
        missing = object()
        explicit = self.ctx.get_config("fish_model", missing) if self.ctx is not None and use_setting else missing
        pinned = canonical_fish_model(self.host("tts.fish-audio.model"))
        requested = canonical_fish_model(model) or canonical_fish_model(explicit)
        return FishModel(pinned, requested, pinned or requested or "s2.1-pro")

    def fish_model(self, model=None):
        return self.resolve_fish_model(model).effective

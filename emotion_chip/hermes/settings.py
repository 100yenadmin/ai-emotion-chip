"""Small injectable boundaries for host config, session context and plugin data."""
import os
from pathlib import Path

DEFAULTS = {
    "backend": "fish", "fish_model": "s2.1-pro", "guide_mode": "generate",
    "signals": "off", "sounds_per_turn": 1, "business_platforms": ["slack", "email"],
    "delivery_log": True,
}
FISH_MODELS = {"s2.1-pro", "s2.1-pro-free", "s2-pro", "drama-3", "s1", "drama-3-preview"}


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
        return self.ctx.get_config(key, default) if self.ctx is not None else default

    def host(self, key, default=None):
        return self.reader(key, default)

    def tts_provider(self):
        return self.host("tts.provider", "edge")

    def host_fish_model(self):
        return self.host("tts.fish-audio.model", "s2.1-pro")

    def fish_model(self, model=None):
        return model if model in FISH_MODELS else self.get("fish_model")

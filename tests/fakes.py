"""Offline host and Fish stand-ins. No Hermes or Fish imports."""
from emotion_chip.hermes._compat import TTSProvider


class FakeContext:
    def __init__(self, settings=None):
        self.settings = dict(settings or {})
        self.state = {}
        self.calls = []
        self.hooks = {}

    def get_config(self, key, default=None):
        return self.settings.get(key, default)

    def set_config(self, key, value):
        self.settings[key] = value

    def register_tts_provider(self, provider):
        self.calls.append(("provider", provider.name))
        self.provider = provider

    def register_system_prompt_section(self, name, content, *, max_chars):
        self.calls.append(("section", name))
        self.section = content
        self.max_chars = max_chars

    def register_skill(self, name, path, description=""):
        self.calls.append(("skill", name))
        self.skill = (path, description)

    def register_hook(self, name, callback):
        self.calls.append(("hook", name))
        self.hooks[name] = callback

    def register_middleware(self, kind, callback):
        self.calls.append(("middleware", kind))
        self.middleware = callback

    def register_command(self, name, handler, description="", args_hint=""):
        self.calls.append(("command", name))
        self.command = handler
        self.command_info = (description, args_hint)


class FakeFish(TTSProvider):
    name = "fish-audio"
    streams_pcm = True
    stream_sample_rate = 24000
    available = True

    def __init__(self):
        self.calls = []
        self.pinned_model = None
        self.effective_models = []

    def is_available(self):
        return self.available

    def synthesize(self, text, output_path, **kwargs):
        self.effective_models.append(self.pinned_model or kwargs.get("model") or "s2.1-pro")
        self.calls.append(("synthesize", text, kwargs))
        return output_path

    def stream(self, text, **kwargs):
        self.effective_models.append(self.pinned_model or kwargs.get("model") or "s2.1-pro")
        self.calls.append(("stream", text, kwargs))
        return iter([b"pcm"])

    def list_voices(self):
        return [{"id": "fixture-voice"}]

    def default_voice(self):
        return "fixture-voice"

    def list_models(self):
        return [{"id": "s2.1-pro"}]

    def warm(self):
        self.calls.append(("warm",))

    def release(self):
        self.calls.append(("release",))

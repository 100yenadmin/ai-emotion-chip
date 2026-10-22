"""Render agent-authored scores and delegate audio to the enabled Fish plugin."""
from dataclasses import asdict
from threading import RLock
import time

from ..model import Cue
from ..render import dialect_for, render
from ..score import parse
from ._compat import TTSProvider
from .log import DeliveryLog
from .settings import Settings, session_env
from .situation import Situation

last_render = {}
last_render_lock = RLock()


def _fish():
    try:
        from agent.tts_registry import get_provider
    except ImportError:
        return None
    return get_provider("fish-audio")


class EmotionChipTTSProvider(TTSProvider):
    name = "emotion-chip"
    display_name = "Hermes Emotion Chip"
    voice_compatible = True

    def __init__(self, settings=None, situation=None, *, env=None, clock=None, data_dir=None):
        self.settings = settings or Settings()
        self.situation = situation or Situation({})
        self.env = env or session_env
        self.clock = clock or time.time
        self.log = DeliveryLog(self.settings, data_dir)
        self._lock = RLock()
        self._carried = {}

    @property
    def streams_pcm(self):
        fish = _fish()
        return fish.streams_pcm if fish is not None else False

    @property
    def stream_sample_rate(self):
        fish = _fish()
        return fish.stream_sample_rate if fish is not None else None

    def is_available(self):
        fish = _fish()
        return fish is not None and fish.is_available()

    def _prepare(self, text, model, speed, streaming=False):
        effective = self.settings.fish_model(model)
        dialect = dialect_for("fish", effective)
        session_id = self.env("HERMES_SESSION_ID")
        platform = self.env("HERMES_SESSION_PLATFORM")
        sounds = self.settings.get("sounds_per_turn")
        if platform in self.settings.get("business_platforms") or self.situation.get("quiet", False):
            sounds = 0
        options = {"sounds_per_turn": sounds, "allow_speakers": False}
        score = parse(text)
        words = [word for sentence in score.sentences for token in sentence.tokens
                 if isinstance(token, Cue) for word in token.words]
        now = self.clock()
        with self._lock:
            self._carried = {k: v for k, v in self._carried.items() if 0 <= now - v[1] <= 120}
            if streaming:
                if not words and session_id and session_id in self._carried:
                    options["params_from"] = self._carried[session_id][0]
            rendered = render(score, dialect, options)
            if session_id:
                self._carried[session_id] = (rendered.group, now)
        final = max(0.5, min(2.0, (speed if speed else 1.0) * rendered.params["speed"]))
        if score.header:
            self.situation.set("register", "[[" + score.header.raw + "]]")
        with last_render_lock:
            last_render.clear()
            last_render.update(session_id=session_id, dialect=rendered.dialect, cue_words=words,
                               group=rendered.group, params=dict(rendered.params),
                               dropped=[asdict(d) for d in rendered.dropped], notes=list(rendered.notes))
        metadata = self.situation.session(session_id)
        metadata["platform"] = platform or metadata.get("platform", "")
        self.log.append(session_id, metadata, rendered, words, len(text), now)
        return rendered, effective, final

    @staticmethod
    def _required_fish():
        fish = _fish()
        if fish is None:
            raise ValueError("Hermes Emotion Chip needs the fish-audio plugin enabled.")
        return fish

    def synthesize(self, text, output_path, *, voice=None, model=None, speed=None, format="mp3", **extra):
        fish = self._required_fish()
        rendered, effective, final = self._prepare(text, model, speed)
        return fish.synthesize(rendered.text, output_path, voice=voice, model=effective, speed=final,
                               format=format, result_metadata=extra.get("result_metadata"))

    def stream(self, text, *, voice=None, model=None, format="pcm", **extra):
        fish = self._required_fish()
        rendered, effective, final = self._prepare(text, model, extra.get("speed"), streaming=True)
        return fish.stream(rendered.text, voice=voice, model=effective, speed=final, format=format)

    def list_voices(self):
        fish = _fish()
        return fish.list_voices() if fish is not None else []

    def default_voice(self):
        fish = _fish()
        return fish.default_voice() if fish is not None else None

    def list_models(self):
        fish = _fish()
        return fish.list_models() if fish is not None else []

    def warm(self):
        fish = _fish()
        return fish.warm() if fish is not None else None

    def release(self):
        fish = _fish()
        return fish.release() if fish is not None else None

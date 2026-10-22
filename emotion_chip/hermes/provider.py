"""Render agent-authored scores and delegate audio to the enabled Fish plugin."""
from dataclasses import asdict
from threading import RLock
import math
import time

from ..data import load_data
from ..model import Cue
from ..render import dialect_for
from ..score import header_cue, parse
from ._compat import TTSProvider
from .fishtags import adapt_all, fish_adapter, settled_render
from .log import DeliveryLog
from .settings import Settings, session_env, session_identity, sounds_on
from .situation import Situation

last_render = {}  # session id -> the newest render's explanation for /chip why (the 50 newest sessions)
last_render_lock = RLock()


def _base_speed(value):
    """Fish's speed rule: any int or finite float counts (the final clamp handles size); anything else is 1.0."""
    if isinstance(value, bool) or not isinstance(value, (int, float)):
        return 1.0
    if isinstance(value, int):
        return float(max(0, min(value, 10)))  # beyond the 0.5-2.0 clamp either way, without float overflow
    return value if math.isfinite(value) else 1.0


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

    def __init__(self, settings=None, situation=None, *, env=None, clock=None, data_dir=None, tag_adapter=None):
        self.settings = settings or Settings()
        self.situation = situation or Situation({})
        self.env = env or session_env
        self.clock = clock or time.time
        self.log = DeliveryLog(self.settings, data_dir)
        self.tag_adapter = tag_adapter
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
        models = self.settings.resolve_fish_model(model)
        dialect = dialect_for("fish", models.effective)
        session_id = session_identity(self.env, self.situation)
        platform = self.env("HERMES_SESSION_PLATFORM")
        options = {"sounds": sounds_on(self.situation), "allow_speakers": False}
        notes = []
        original_chars = len(text)
        # Fish performs (cue) parens natively on S1 and adapts them to tags again on S2. Translating them on every Fish
        # model (drama-3 and the tagless S1 route too) renders each paren cue like a bracket cue.
        try:
            adapt = self.tag_adapter or fish_adapter(_fish())
            text = adapt_all(text, adapt)
        except Exception:
            adapt = None
            notes.append("Fish adapt_tags unavailable; skipped pre-parse adaptation")
        data = load_data()
        score = parse(text, data)
        opener = header_cue(score.header)
        words = (list(opener.words) if opener else []) + [
            word for sentence in score.sentences for token in sentence.tokens if isinstance(token, Cue) for word in token.words]
        has_cue = any(isinstance(token, Cue) for sentence in score.sentences for token in sentence.tokens)
        now = self.clock()
        with self._lock:
            self._carried = {k: v for k, v in self._carried.items() if 0 <= now - v[1] <= 120}
            if streaming and not has_cue and score.header is None and session_id and session_id in self._carried:
                options["params_from"] = self._carried[session_id][0]
            rendered, added = settled_render(score, dialect, options, data, adapt)
            # A carried piece refreshes the carry too, so a long reply keeps its opening cue's params.
            if session_id and rendered.group is not None:
                self._carried[session_id] = (rendered.group, now)
        words += added
        base = _base_speed(self.settings.host("tts.fish-audio.speed", 1.0) if speed is None else speed)
        final = max(0.5, min(2.0, base * rendered.params.get("speed", 1.0)))
        if models.pinned and models.requested and models.pinned != models.requested:
            notes.append(f"Fish pinned model wins: {models.pinned} over requested {models.requested}")
        rendered.notes.extend(notes)
        self.situation.update(session_id, lambda row: row.update(
            {"register": "[[" + score.header.raw + "]]"} if score.header else {}), platform=platform or None)
        with last_render_lock:
            last_render.pop(session_id, None)
            last_render[session_id] = dict(session_id=session_id, fish_model=models.effective, dialect=rendered.dialect,
                                           cue_words=words, group=rendered.group, params=dict(rendered.params),
                                           dropped=[asdict(d) for d in rendered.dropped], notes=list(rendered.notes))
            while len(last_render) > 50:
                last_render.pop(next(iter(last_render)))
        metadata = self.situation.session(session_id)
        metadata["platform"] = platform or metadata.get("platform", "")
        self.log.append(session_id, metadata, rendered, words, original_chars, now, models.effective)
        return rendered, models.requested, final

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

"""Render agent-authored scores and send them to the selected voice backend."""
from dataclasses import asdict
from threading import RLock
import math
import os
import time

from ..data import load_data
from ..model import Cue
from ..render import dialect_for
from ..score import header_cue, parse
from ._compat import TTSProvider
from .eleven import ElevenClient
from .fishtags import adapt_all, fish_adapter, settled_render
from .log import DeliveryLog
from .settings import ELEVEN_MODELS, Settings, session_env, session_identity, sounds_on
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

    def __init__(self, settings=None, situation=None, *, env=None, clock=None, data_dir=None, tag_adapter=None,
                 opener=None, timeout=60):
        self.settings = settings or Settings()
        self.situation = situation or Situation({})
        self.env = env or session_env
        self.clock = clock or time.time
        self.log = DeliveryLog(self.settings, data_dir)
        self.tag_adapter = tag_adapter
        self._lock = RLock()
        self._carried = {}
        self._request_ids = {}
        self.eleven = ElevenClient(opener=opener, timeout=timeout)

    @property
    def max_text_length(self):
        if self.settings.get("backend") == "eleven":
            return 10000 if self.settings.eleven_model() == "eleven_v4" else 5000
        return getattr(_fish(), "max_text_length", None)

    @property
    def streams_pcm(self):
        if self.settings.get("backend") == "eleven":
            return True
        fish = _fish()
        return fish.streams_pcm if fish is not None else False

    @property
    def stream_sample_rate(self):
        if self.settings.get("backend") == "eleven":
            return 24000
        fish = _fish()
        return fish.stream_sample_rate if fish is not None else None

    def is_available(self):
        if self.settings.get("backend") == "eleven":
            return bool(os.environ.get("ELEVENLABS_API_KEY") and self.settings.eleven_voice(self.settings.call_voice()))
        fish = _fish()
        return fish is not None and fish.is_available()

    def _prepare(self, text, model, speed, streaming=False):
        backend = self.settings.get("backend")
        models = self.settings.resolve_fish_model(model) if backend == "fish" else None
        effective = models.effective if models else self.settings.eleven_model()
        dialect = dialect_for(backend, effective)
        session_id = session_identity(self.env, self.situation)
        platform = self.env("HERMES_SESSION_PLATFORM")
        options = {"sounds": sounds_on(self.situation), "allow_speakers": False}
        notes = []
        original_chars = len(text)
        # Fish performs (cue) parens natively on S1 and adapts them to tags again on S2. Translating them on every Fish
        # model (drama-3 and the tagless S1 route too) renders each paren cue like a bracket cue.
        adapt = None
        if backend == "fish":
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
        final = None
        if models:
            base = _base_speed(self.settings.host("tts.fish-audio.speed", 1.0) if speed is None else speed)
            final = max(0.5, min(2.0, base * rendered.params.get("speed", 1.0)))
        if models and models.pinned and models.requested and models.pinned != models.requested:
            notes.append(f"Fish pinned model wins: {models.pinned} over requested {models.requested}")
        rendered.notes.extend(notes)
        self.situation.update(session_id, lambda row: row.update(
            {"register": "[[" + score.header.raw + "]]"} if score.header else {}), platform=platform or None)
        with last_render_lock:
            last_render.pop(session_id, None)
            last_render[session_id] = dict(session_id=session_id, fish_model=effective, backend=backend, dialect=rendered.dialect,
                                           cue_words=words, group=rendered.group, params=dict(rendered.params),
                                           dropped=[asdict(d) for d in rendered.dropped], notes=list(rendered.notes))
            while len(last_render) > 50:
                last_render.pop(next(iter(last_render)))
        metadata = self.situation.session(session_id)
        metadata["platform"] = platform or metadata.get("platform", "")
        self.log.append(session_id, metadata, rendered, words, original_chars, now, effective)
        if backend == "eleven" and len(rendered.text) > self.max_text_length:
            raise ValueError(f"ElevenLabs text length {len(rendered.text)} exceeds {self.max_text_length} for {effective}.")
        return rendered, models.requested if models else effective, final

    def _eleven_params(self, rendered):
        params = dict(rendered.params)
        for key in ("style", "speed"):
            value = self.settings.get("eleven_" + key)
            if value is not None:
                params[key] = value
        return params

    @staticmethod
    def _required_fish():
        fish = _fish()
        if fish is None:
            raise ValueError("Hermes Emotion Chip needs the fish-audio plugin enabled.")
        return fish

    def synthesize(self, text, output_path, *, voice=None, model=None, speed=None, format="mp3", **extra):
        if self.settings.get("backend") == "eleven":
            session_id = session_identity(self.env, self.situation)
            rendered, effective, _ = self._prepare(text, model, speed)
            stitch = effective in {"eleven_v4", "eleven_v4_turbo"} and bool(session_id)
            with self._lock:
                now = self.clock()
                self._request_ids = {k: [(rid, ts) for rid, ts in ids if 0 <= now - ts < 7200]
                                     for k, ids in self._request_ids.items()}
                self._request_ids = {k: ids for k, ids in self._request_ids.items() if ids}
                previous = [rid for rid, _ in self._request_ids.get(session_id, [])] if stitch else []
            request_id = self.eleven.synthesize(rendered.text, output_path, voice=self.settings.eleven_voice(voice),
                                                model=effective, params=self._eleven_params(rendered), format=format,
                                                previous_request_ids=previous)
            if stitch and request_id:
                with self._lock:
                    self._request_ids[session_id] = (self._request_ids.get(session_id, []) +
                                                     [(request_id, self.clock())])[-3:]
            return output_path
        fish = self._required_fish()
        rendered, effective, final = self._prepare(text, model, speed)
        return fish.synthesize(rendered.text, output_path, voice=voice, model=effective, speed=final,
                               format=format, result_metadata=extra.get("result_metadata"))

    def stream(self, text, *, voice=None, model=None, format="pcm", **extra):
        if self.settings.get("backend") == "eleven":
            rendered, effective, _ = self._prepare(text, model, extra.get("speed"), streaming=True)
            return self.eleven.stream(rendered.text, voice=self.settings.eleven_voice(voice), model=effective,
                                      params=self._eleven_params(rendered))
        fish = self._required_fish()
        rendered, effective, final = self._prepare(text, model, extra.get("speed"), streaming=True)
        return fish.stream(rendered.text, voice=voice, model=effective, speed=final, format=format)

    def list_voices(self):
        if self.settings.get("backend") == "eleven":
            return []
        fish = _fish()
        return fish.list_voices() if fish is not None else []

    def default_voice(self):
        if self.settings.get("backend") == "eleven":
            return self.settings.eleven_voice(self.settings.call_voice())
        fish = _fish()
        return fish.default_voice() if fish is not None else None

    def list_models(self):
        if self.settings.get("backend") == "eleven":
            return [{"id": model} for model in ELEVEN_MODELS]
        fish = _fish()
        return fish.list_models() if fish is not None else []

    def warm(self):
        if self.settings.get("backend") == "eleven":
            return None
        fish = _fish()
        return fish.warm() if fish is not None else None

    def release(self):
        if self.settings.get("backend") == "eleven":
            return None
        fish = _fish()
        return fish.release() if fish is not None else None

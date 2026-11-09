"""One guarded wiring point; no credentials, audio transport or network at load."""
import logging
import os
from pathlib import Path
import tempfile
import threading

from ..cli import add_commands, run
from ..data import DIALECTS, load_data
from ..guide import build_prompt_section, build_skill
from ..render import dialect_for
from .commands import make_handler
from .hooks import HOOKS, Hooks
from .middleware import make_middleware
from .provider import EmotionChipTTSProvider
from .settings import Settings, plugin_data_dir, session_env
from .situation import Situation

logger = logging.getLogger(__name__)


def model_key(model):
    name = str(model or "").rsplit("/", 1)[-1]
    for key in ("claude-opus-5-5", "claude-fable-5-1"):
        if name.startswith(key):
            return key
    return "default"


def register_all(ctx, *, reader=None, data_dir=None, env=None, clock=None):
    settings = Settings(ctx, reader)
    try:
        state = getattr(ctx, "state", None)
    except Exception:
        state = None
    situation = Situation(state if state is not None else {}, clock)
    directory = data_dir or plugin_data_dir
    environment = env or session_env
    provider = EmotionChipTTSProvider(settings, situation, env=environment, clock=clock, data_dir=directory)
    data = load_data()
    # Hermes passes tts.model as the call's model on the provider path; the prompt and skill teach that dialect.
    dialect = dialect_for(settings.get("backend"), settings.model(settings.host("tts.model")))
    for name in DIALECTS:
        for key in data.models:
            try:
                if len(build_prompt_section(name, key, data)) > 3800:
                    logger.warning("Emotion Chip prompt over 3800 chars: %s/%s", name, key)
            except ValueError:
                logger.warning("Emotion Chip prompt over 3800 chars: %s/%s", name, key)

    written = [None]
    skill_lock = threading.Lock()

    def write_skill(dialect):
        text = build_skill(dialect, "default", data)
        path = Path(directory()) / "skills" / "emotion-chip-voice" / "SKILL.md"
        path.parent.mkdir(parents=True, exist_ok=True)
        temp = tempfile.NamedTemporaryFile(mode="w", dir=path.parent, encoding="utf-8", delete=False)
        try:
            with temp:
                temp.write(text + "\n")
            # NamedTemporaryFile is private (0600); keep the mode an ordinary write gave the skill.
            os.chmod(temp.name, path.stat().st_mode & 0o777 if path.exists() else 0o644)
            os.replace(temp.name, path)
        except BaseException:
            # Leave no partial file next to the skill when the write or the swap fails.
            Path(temp.name).unlink(missing_ok=True)
            raise
        return text

    def content_fn(info):
        if settings.get("guide_mode") != "generate":
            return ""
        current = dialect_for(settings.get("backend"), settings.model(settings.host("tts.model")))
        with skill_lock:
            if written[0] is not None and current != written[0]:
                try:
                    write_skill(current)
                    written[0] = current
                except OSError:
                    logger.warning("Emotion Chip skill rewrite failed", exc_info=True)
        if settings.tts_provider() != "emotion-chip":
            return ""
        return build_prompt_section(current, model_key(info.get("model")), data)

    if hasattr(ctx, "register_tts_provider"):
        ctx.register_tts_provider(provider)
    if hasattr(ctx, "register_system_prompt_section"):
        ctx.register_system_prompt_section("emotion-chip", content_fn, max_chars=4000)
    if settings.get("guide_mode") == "generate" and hasattr(ctx, "register_skill"):
        text = write_skill(dialect)
        written[0] = dialect
        path = Path(directory()) / "skills" / "emotion-chip-voice" / "SKILL.md"
        frontmatter = text.split("---", 2)[1]
        description = next((line.partition(":")[2].strip().strip("\"'") for line in frontmatter.splitlines()
                            if line.startswith("description:")), "")
        ctx.register_skill("emotion-chip-voice", path, description=description)
    voice_replies = None  # set only when the voice-replies line is registered; /chip status reports it
    if hasattr(ctx, "register_hook"):
        hooks = Hooks(settings, situation, environment, start_turn=provider.start_turn)
        # pre_gateway_dispatch only serves the voice-replies line; leave Hermes's ingress untouched where it cannot apply.
        voice_line = settings.get("guide_mode") == "generate" and settings.tts_provider() == "emotion-chip"
        for name in HOOKS:
            if name != "pre_gateway_dispatch" or voice_line:
                ctx.register_hook(name, getattr(hooks, name))
        if voice_line:
            voice_replies = hooks.voice_replies
    if hasattr(ctx, "register_middleware"):
        ctx.register_middleware("tool_request", make_middleware(settings, situation, environment))
    if hasattr(ctx, "register_command"):
        ctx.register_command("chip", make_handler(settings, situation, environment, voice_replies),
                             description="Hermes Emotion Chip voice delivery",
                             args_hint="[status|why|quiet|register]")
    if hasattr(ctx, "register_cli_command"):
        ctx.register_cli_command("chip", "Hermes Emotion Chip: render a delivery score, print the guide",
                                 add_commands, run,
                                 description="Render a delivery score for ElevenLabs or Fish Audio, or print the "
                                             "delivery guide the agent gets. Offline: no keys, no network.")

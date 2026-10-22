"""One guarded wiring point; no credentials, audio transport or network at load."""
import logging
from pathlib import Path

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

    def content_fn(info):
        if settings.get("guide_mode") != "generate" or settings.tts_provider() != "emotion-chip":
            return ""
        return build_prompt_section(dialect_for(settings.get("backend"), settings.model(settings.host("tts.model"))),
                                    model_key(info.get("model")), data)

    if hasattr(ctx, "register_tts_provider"):
        ctx.register_tts_provider(provider)
    if hasattr(ctx, "register_system_prompt_section"):
        ctx.register_system_prompt_section("emotion-chip", content_fn, max_chars=4000)
    if settings.get("guide_mode") == "generate" and hasattr(ctx, "register_skill"):
        text = build_skill(dialect, "default", data)
        path = Path(directory()) / "skills" / "emotion-chip-voice" / "SKILL.md"
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(text + "\n", encoding="utf-8")
        frontmatter = text.split("---", 2)[1]
        description = next((line.partition(":")[2].strip().strip("\"'") for line in frontmatter.splitlines()
                            if line.startswith("description:")), "")
        ctx.register_skill("emotion-chip-voice", path, description=description)
    if hasattr(ctx, "register_hook"):
        hooks = Hooks(settings, situation, environment, start_turn=provider.start_turn)
        for name in HOOKS:
            ctx.register_hook(name, getattr(hooks, name))
    if hasattr(ctx, "register_middleware"):
        ctx.register_middleware("tool_request", make_middleware(settings, situation, environment))
    if hasattr(ctx, "register_command"):
        ctx.register_command("chip", make_handler(settings, situation, environment),
                             description="Hermes Emotion Chip voice delivery",
                             args_hint="[status|why|quiet|register]")

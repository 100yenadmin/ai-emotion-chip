"""Hermes plugin entry point."""


def register(ctx):
    # Hermes imports a directory plugin as hermes_plugins.<slug> without adding it to sys.path: import relatively.
    from .emotion_chip.hermes.registration import register_all
    register_all(ctx)

"""Hermes plugin entry point."""


def register(ctx):
    from emotion_chip.hermes.registration import register_all
    register_all(ctx)

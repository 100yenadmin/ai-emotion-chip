"""Use Hermes's provider base when installed, a minimal base for offline tests."""
try:
    from agent.tts_provider import TTSProvider
except ImportError:
    class TTSProvider:
        def default_model(self):
            models = self.list_models()
            return models[0]["id"] if models else None

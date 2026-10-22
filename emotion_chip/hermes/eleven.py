"""Small standard-library ElevenLabs transport; credentials are read only at call time."""
import json
import os
from pathlib import Path
from urllib.error import HTTPError
from urllib.parse import quote
from urllib.request import Request, urlopen
import wave

FORMATS = {"mp3": "mp3_44100_128", "ogg": "opus_48000_64", "opus": "opus_48000_64",
           "pcm": "pcm_24000", "wav": "pcm_24000"}


class ElevenClient:
    def __init__(self, *, opener=None, timeout=60):
        self.opener = opener or urlopen
        self.timeout = timeout

    def _open(self, text, voice, model, params, output_format, *, streaming=False, previous_request_ids=()):
        if not voice:
            raise ValueError("ElevenLabs needs a voice id (voice, eleven_voice_id or tts.elevenlabs.voice_id).")
        key = os.environ.get("ELEVENLABS_API_KEY", "")
        if not key:
            raise ValueError("ElevenLabs needs ELEVENLABS_API_KEY in the environment.")
        payload = {"text": text, "model_id": model, "voice_settings": dict(params)}
        if previous_request_ids and not streaming and model in {"eleven_v4", "eleven_v4_turbo"}:
            payload["previous_request_ids"] = list(previous_request_ids)
        suffix = "/stream" if streaming else ""
        url = f"https://api.elevenlabs.io/v1/text-to-speech/{quote(voice, safe='')}{suffix}?output_format={output_format}"
        request = Request(url, data=json.dumps(payload).encode("utf-8"), method="POST",
                          headers={"xi-api-key": key, "Content-Type": "application/json", "Accept": "audio/*"})
        try:
            return self.opener(request, timeout=self.timeout)
        except HTTPError as error:
            with error:
                body = error.read().decode("utf-8", errors="replace").replace(key, "[redacted]")[:200]
            raise RuntimeError(f"ElevenLabs HTTP {error.code}: {body}") from None

    def synthesize(self, text, output_path, *, voice, model, params, format="mp3", previous_request_ids=()):
        if format not in FORMATS:
            raise ValueError(f"Unknown ElevenLabs format: {format}")
        with self._open(text, voice, model, params, FORMATS[format],
                        previous_request_ids=previous_request_ids) as response:
            audio, request_id = response.read(), response.headers.get("request-id")
        if format == "wav":
            with wave.open(str(output_path), "wb") as handle:
                handle.setnchannels(1)
                handle.setsampwidth(2)
                handle.setframerate(24000)
                handle.writeframes(audio)
        else:
            Path(output_path).write_bytes(audio)
        return request_id

    def stream(self, text, *, voice, model, params):
        with self._open(text, voice, model, params, "pcm_24000", streaming=True) as response:
            while chunk := response.read(4096):
                yield chunk

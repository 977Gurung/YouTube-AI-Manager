"""Stand-in for chatterbox.tts.ChatterboxTTS used by the sandbox tests: same call signature,
returns a tone whose length follows the text and whose loudness follows `exaggeration`.
Every call is appended to $FAKE_CB_LOG so tests can check what the worker asked for."""
import json
import os

import numpy as np


def _log(entry):
    path = os.environ.get("FAKE_CB_LOG")
    if path:
        with open(path, "a", encoding="utf-8") as f:
            f.write(json.dumps(entry) + "\n")


class ChatterboxTTS:
    def __init__(self, device):
        self.device, self.sr = device, 24000

    @classmethod
    def from_pretrained(cls, device):
        if os.environ.get("FAKE_CB_FAIL") == "load":
            raise RuntimeError("CUDA out of memory (simulated)")
        _log({"event": "load", "device": device})
        return cls(device)

    def generate(self, text, audio_prompt_path=None, exaggeration=0.5, cfg_weight=0.5, temperature=0.8):
        if os.environ.get("FAKE_CB_FAIL") == "generate":
            raise RuntimeError("generation failed (simulated)")
        _log({"event": "generate", "text": text, "exaggeration": exaggeration, "cfg_weight": cfg_weight,
              "temperature": temperature, "reference": audio_prompt_path})
        n = int(self.sr * max(0.4, len(text) / 15.0))
        t = np.arange(n) / self.sr
        return (np.sin(2 * np.pi * 220 * t) * min(0.9, 0.3 + 0.5 * exaggeration)).astype("float32")[None, :]

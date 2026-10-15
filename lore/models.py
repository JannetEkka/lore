"""Which Gemini models the key can use, and the order to fall back through when one hits its free limit."""
import os
import re
import time

import requests

API = "https://generativelanguage.googleapis.com/v1beta"
NOT_CHAT = ("tts", "image", "banana", "lyria", "robotics", "computer-use", "transcribe", "deep-research",
            "antigravity", "customtools", "omni", "embedding", "live", "audio", "argon")

_cache = {"at": 0, "models": []}


def _rank(name):
    """Newest Flash first, then Flash-Lite, then Gemma, then Pro (Pro has little or no free quota)."""
    m = re.search(r"(\d+(?:\.\d+)?)", name.replace("gemma-", "gemma"))
    version = float(m.group(1)) if m and not name.startswith("gemma") else 0.0
    if name.startswith("gemma"):
        family = 3
    elif "flash-lite" in name or "flash lite" in name:
        family = 1
    elif "flash" in name:
        family = 0
    else:
        family = 4
    return (family, "latest" in name, "preview" in name, -version, name)


def list_models(key=None, refresh=False):
    server_key = os.environ.get("GEMINI_API_KEY")
    key = key or server_key
    if key == server_key and not refresh and _cache["models"] and time.time() - _cache["at"] < 3600:
        return _cache["models"]
    r = requests.get(f"{API}/models", params={"pageSize": 200}, headers={"x-goog-api-key": key}, timeout=20)
    r.raise_for_status()
    names = [m["name"].split("/")[-1] for m in r.json().get("models", [])
             if "generateContent" in m.get("supportedGenerationMethods", [])]
    names = [n for n in names if n.startswith(("gemini-", "gemma-")) and not any(x in n for x in NOT_CHAT)]
    names.sort(key=_rank)
    if key == server_key:
        _cache.update(at=time.time(), models=names)
    return names


def fallback_order(chosen, available):
    """The chosen model first, then the rest in rank order."""
    rest = [m for m in available if m != chosen]
    return ([chosen] if chosen else []) + rest

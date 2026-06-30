"""
AI provider adapters (doc sections 4 & 10): paste a Gemini or ChatGPT key, select
a model. Each adapter exposes one method -- generate(system_prompt, user_prompt) ->
raw text -- and the service parses the JSON. google/openai SDKs are imported lazily
so the engine and the offline test suite run without them installed.
"""

import logging

from apps.brand_settings.models import BrandSettings

logger = logging.getLogger(__name__)

DEFAULT_MODELS = {
    BrandSettings.PROVIDER_GEMINI: "gemini-2.5-flash",
    BrandSettings.PROVIDER_CHATGPT: "gpt-4o-mini",
    BrandSettings.PROVIDER_GROQ: "llama-3.3-70b-versatile",
}

GROQ_BASE_URL = "https://api.groq.com/openai/v1"


class ProviderError(RuntimeError):
    pass


class BaseProvider:
    def generate(self, system_prompt, user_prompt):  # pragma: no cover - interface
        raise NotImplementedError

    def generate_text(self, system_prompt, user_prompt):  # pragma: no cover
        """Plain-text generation (for reply drafting). Defaults to generate()."""
        return self.generate(system_prompt, user_prompt)


class GeminiProvider(BaseProvider):
    def __init__(self, api_key, model):
        self.api_key = api_key
        self.model = model or DEFAULT_MODELS[BrandSettings.PROVIDER_GEMINI]

    def generate(self, system_prompt, user_prompt):
        import google.generativeai as genai  # type: ignore[import]

        genai.configure(api_key=self.api_key)
        model = genai.GenerativeModel(
            self.model, system_instruction=system_prompt
        )
        resp = model.generate_content(
            user_prompt,
            generation_config={"response_mime_type": "application/json"},
        )
        return resp.text

    def generate_text(self, system_prompt, user_prompt):
        import google.generativeai as genai  # type: ignore[import]

        genai.configure(api_key=self.api_key)
        model = genai.GenerativeModel(self.model, system_instruction=system_prompt)
        return model.generate_content(user_prompt).text


class ChatGPTProvider(BaseProvider):
    def __init__(self, api_key, model):
        self.api_key = api_key
        self.model = model or DEFAULT_MODELS[BrandSettings.PROVIDER_CHATGPT]

    def generate(self, system_prompt, user_prompt):
        try:
            import importlib

            openai = importlib.import_module("openai")
            OpenAI = getattr(openai, "OpenAI")
        except Exception:  # pragma: no cover - optional dependency
            raise ProviderError("OpenAI SDK is not installed")

        client = OpenAI(api_key=self.api_key)
        resp = client.chat.completions.create(
            model=self.model,
            messages=[
                {"role": "system", "content": system_prompt},
                {"role": "user", "content": user_prompt},
            ],
            response_format={"type": "json_object"},
            temperature=0,
        )
        return resp.choices[0].message.content

    def generate_text(self, system_prompt, user_prompt):
        try:
            import importlib

            openai = importlib.import_module("openai")
            OpenAI = getattr(openai, "OpenAI")
        except Exception:  # pragma: no cover - optional dependency
            raise ProviderError("OpenAI SDK is not installed")

        client = OpenAI(api_key=self.api_key)
        resp = client.chat.completions.create(
            model=self.model,
            messages=[
                {"role": "system", "content": system_prompt},
                {"role": "user", "content": user_prompt},
            ],
            temperature=0.3,
        )
        return resp.choices[0].message.content


class GroqProvider(BaseProvider):
    """Groq (OpenAI-compatible API) -- e.g. llama-3.3-70b-versatile. Fast + generous
    free tier. Uses the OpenAI SDK pointed at Groq's endpoint."""

    def __init__(self, api_key, model):
        self.api_key = api_key
        self.model = model or DEFAULT_MODELS[BrandSettings.PROVIDER_GROQ]

    def _client(self):
        try:
            import importlib

            openai = importlib.import_module("openai")
            OpenAI = getattr(openai, "OpenAI")
        except Exception:  # pragma: no cover - optional dependency
            raise ProviderError("OpenAI SDK is not installed")

        return OpenAI(api_key=self.api_key, base_url=GROQ_BASE_URL)

    def generate(self, system_prompt, user_prompt):
        resp = self._client().chat.completions.create(
            model=self.model,
            messages=[
                {"role": "system", "content": system_prompt},
                {"role": "user", "content": user_prompt},
            ],
            response_format={"type": "json_object"},
            temperature=0,
        )
        return resp.choices[0].message.content

    def generate_text(self, system_prompt, user_prompt):
        resp = self._client().chat.completions.create(
            model=self.model,
            messages=[
                {"role": "system", "content": system_prompt},
                {"role": "user", "content": user_prompt},
            ],
            temperature=0.3,
        )
        return resp.choices[0].message.content


def get_provider(settings):
    """Build a provider from a BrandSettings row, or None if no key is configured.

    Returning None (rather than raising) lets callers degrade to "leave the ticket
    for an agent" when a brand hasn't set up AI yet.
    """
    try:
        import importlib

        dj = importlib.import_module("django.conf").settings
    except Exception:  # pragma: no cover - optional dependency
        dj = type("DJSettings", (), {})()

    brand_key = settings.ai_api_key if settings else ""
    model = (settings.ai_model if settings else "") or ""

    # Per-brand key wins (provider chosen in BrandSettings).
    if brand_key:
        provider = settings.ai_provider if settings else ""
        if provider == BrandSettings.PROVIDER_GROQ:
            return GroqProvider(brand_key, model)
        if provider == BrandSettings.PROVIDER_CHATGPT:
            return ChatGPTProvider(brand_key, model)
        return GeminiProvider(brand_key, model)

    # Otherwise fall back to a GLOBAL key from .env. Preference: Groq -> Gemini ->
    # ChatGPT, so a single key powers every brand.
    if getattr(dj, "GROQ_API_KEY", ""):
        return GroqProvider(dj.GROQ_API_KEY, getattr(dj, "GROQ_MODEL", ""))
    if getattr(dj, "GEMINI_API_KEY", ""):
        return GeminiProvider(dj.GEMINI_API_KEY, getattr(dj, "GEMINI_MODEL", ""))
    if getattr(dj, "OPENAI_API_KEY", ""):
        return ChatGPTProvider(dj.OPENAI_API_KEY, getattr(dj, "OPENAI_MODEL", ""))
    return None

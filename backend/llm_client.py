import json
from urllib import error, request
from urllib.parse import quote

OPENCODE_PLANS = {
    "zen": {
        "baseUrl": "https://opencode.ai/zen/v1",
        "model": "big-pickle",
        "providerId": "opencode",
        "chatModels": frozenset({"big-pickle"}),
    },
    "go": {
        "baseUrl": "https://opencode.ai/zen/go/v1",
        "model": "kimi-k3",
        "providerId": "opencode-go",
        "chatModels": frozenset({"kimi-k3", "kimi-k2.5"}),
    },
}

PROVIDER_PRESETS = {
    "OpenAI": {
        "baseUrl": "https://api.openai.com/v1",
        "model": "gpt-4o-mini",
        "requiresApiKey": True,
    },
    "Anthropic": {
        "baseUrl": "https://api.anthropic.com/v1",
        "model": "claude-3-5-haiku-latest",
        "requiresApiKey": True,
    },
    "Gemini": {
        "baseUrl": "https://generativelanguage.googleapis.com/v1beta",
        "model": "gemini-2.5-flash",
        "requiresApiKey": True,
    },
    "OpenRouter": {
        "baseUrl": "https://openrouter.ai/api/v1",
        "model": "openai/gpt-4o-mini",
        "requiresApiKey": True,
    },
    "Ollama": {
        "baseUrl": "http://localhost:11434",
        "cloudBaseUrl": "https://ollama.com",
        "model": "llama3.2",
        "requiresApiKey": False,
    },
    "OpenCode": {
        "baseUrl": "https://opencode.ai/zen/v1",
        "model": "big-pickle",
        "requiresApiKey": True,
    },
}


def validate_provider_url(provider, base_url, plan="zen"):
    """Accept only documented endpoints before attaching stored credentials."""
    if provider not in PROVIDER_PRESETS:
        raise ValueError("Unsupported LLM provider")
    if not isinstance(base_url, str):
        raise ValueError("LLM endpoint must use its provider preset")
    allowed = {PROVIDER_PRESETS[provider]["baseUrl"]}
    if provider == "OpenCode":
        if plan not in OPENCODE_PLANS:
            raise ValueError("Unsupported OpenCode plan")
        allowed = {OPENCODE_PLANS[plan]["baseUrl"]}
    if provider == "Ollama":
        allowed.add(PROVIDER_PRESETS[provider]["cloudBaseUrl"])
    normalized = base_url.rstrip("/")
    if normalized not in allowed:
        raise ValueError("LLM endpoint must use its provider preset")
    return normalized


class _NoCredentialRedirect(request.HTTPRedirectHandler):
    def redirect_request(self, req, fp, code, msg, headers, newurl):
        raise error.HTTPError(
            req.full_url, code, "LLM redirects are disabled", headers, fp
        )


def _urlopen(req, timeout):
    return request.build_opener(_NoCredentialRedirect()).open(req, timeout=timeout)


class OpenAICompatibleClient:
    @staticmethod
    def _resolve_base_url(provider, base_url, api_key, plan="zen"):
        base_url = validate_provider_url(provider, base_url, plan)
        if provider == "Ollama" and api_key:
            return PROVIDER_PRESETS["Ollama"]["cloudBaseUrl"]
        return base_url

    @staticmethod
    def _content(body, path):
        try:
            content = body
            for key in path:
                content = content[key]
            return content
        except (KeyError, IndexError, TypeError):
            raise RuntimeError("LLM response was malformed")

    def _request(self, url, api_key, payload=None, headers=None, method="GET"):
        request_headers = {"Accept": "application/json", **(headers or {})}
        data = None
        if payload is not None:
            data = json.dumps(payload).encode()
            request_headers["Content-Type"] = "application/json"
        req = request.Request(url, data=data, headers=request_headers, method=method)
        try:
            with _urlopen(req, timeout=30) as response:
                return json.loads(response.read().decode())
        except error.HTTPError as exc:
            raise RuntimeError(f"LLM request failed with HTTP {exc.code}") from exc
        except (TimeoutError, error.URLError) as exc:
            if isinstance(exc, TimeoutError):
                message = "LLM request timed out"
            else:
                message = f"LLM request failed: {exc.reason}"
            raise RuntimeError(message) from exc
        except (json.JSONDecodeError, UnicodeDecodeError) as exc:
            raise RuntimeError("LLM response was not valid JSON") from exc

    def _headers(self, provider, api_key):
        if provider == "Anthropic":
            return {"x-api-key": api_key, "anthropic-version": "2023-06-01"}
        if provider == "Gemini":
            return {"x-goog-api-key": api_key}
        if api_key:
            return {"Authorization": f"Bearer {api_key}"}
        return {}

    def generate(self, base_url, api_key, model, prompt, provider="OpenAI", plan="zen"):
        if provider == "OpenCode":
            opencode_plan = OPENCODE_PLANS.get(plan, OPENCODE_PLANS["zen"])
            if model not in opencode_plan["chatModels"]:
                raise RuntimeError(
                    f"Model '{model}' is not available for OpenCode plan '{plan}'"
                )
        base_url = self._resolve_base_url(provider, base_url, api_key, plan)
        headers = self._headers(provider, api_key)
        if provider == "Anthropic":
            body = self._request(
                f"{base_url}/messages",
                api_key,
                {
                    "model": model,
                    "max_tokens": 1024,
                    "messages": [{"role": "user", "content": prompt}],
                },
                headers,
                "POST",
            )
            content = self._content(body, ("content", 0, "text"))
        elif provider == "Gemini":
            body = self._request(
                f"{base_url}/models/{quote(model, safe='')}:generateContent",
                api_key,
                {"contents": [{"role": "user", "parts": [{"text": prompt}]}]},
                headers,
                "POST",
            )
            content = self._content(
                body, ("candidates", 0, "content", "parts", 0, "text")
            )
        elif provider == "Ollama":
            body = self._request(
                f"{base_url}/api/chat",
                api_key,
                {
                    "model": model,
                    "messages": [{"role": "user", "content": prompt}],
                    "stream": False,
                },
                headers,
                "POST",
            )
            content = self._content(body, ("message", "content"))
        else:
            body = self._request(
                f"{base_url}/chat/completions",
                api_key,
                {"model": model, "messages": [{"role": "user", "content": prompt}]},
                headers,
                "POST",
            )
            content = self._content(body, ("choices", 0, "message", "content"))

        if not isinstance(content, str) or not content:
            raise RuntimeError("LLM response was malformed")
        return content

    def discover_models(self, provider, base_url, api_key, plan="zen"):
        opencode_plan = OPENCODE_PLANS.get(plan, OPENCODE_PLANS["zen"])
        base_url = self._resolve_base_url(provider, base_url, api_key, plan)
        if provider == "OpenCode":
            base_url = opencode_plan["baseUrl"]
        if provider == "Ollama":
            body = self._request(
                f"{base_url}/api/tags",
                api_key,
                headers=self._headers(provider, api_key),
            )
            models = [item.get("name") for item in body.get("models", [])]
        elif provider == "Gemini":
            body = self._request(
                f"{base_url}/models", api_key, headers=self._headers(provider, api_key)
            )
            models = [
                item.get("name", "").removeprefix("models/")
                for item in body.get("models", [])
            ]
        else:
            body = self._request(
                f"{base_url}/models",
                api_key,
                headers={}
                if provider == "OpenCode"
                else self._headers(provider, api_key),
            )
            catalog = body.get("data", [])
            if provider == "OpenCode":
                catalog = [
                    item
                    for item in catalog
                    if isinstance(item, dict)
                    and item.get("id") in opencode_plan["chatModels"]
                ]
            models = [item.get("id") for item in catalog]
        return [model for model in models if model]

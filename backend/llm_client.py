import json
from urllib import error, request

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
        "model": "llama3.2",
        "requiresApiKey": False,
    },
    "OpenCode": {
        "baseUrl": "https://opencode.ai/zen/v1",
        "model": "big-pickle",
        "requiresApiKey": True,
    },
}


class OpenAICompatibleClient:
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
            with request.urlopen(req, timeout=30) as response:
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
            return {}
        if api_key:
            return {"Authorization": f"Bearer {api_key}"}
        return {}

    def generate(self, base_url, api_key, model, prompt, provider="OpenAI"):
        base_url = base_url.rstrip("/")
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
                f"{base_url}/models/{model}:generateContent?key={api_key}",
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

    def discover_models(self, provider, base_url, api_key):
        base_url = base_url.rstrip("/")
        if provider == "Ollama":
            body = self._request(f"{base_url}/api/tags", api_key, headers={})
            models = [item.get("name") for item in body.get("models", [])]
        elif provider == "Gemini":
            body = self._request(
                f"{base_url}/models?key={api_key}", api_key, headers={}
            )
            models = [
                item.get("name", "").removeprefix("models/")
                for item in body.get("models", [])
            ]
        else:
            body = self._request(
                f"{base_url}/models", api_key, headers=self._headers(provider, api_key)
            )
            models = [item.get("id") for item in body.get("data", [])]
        return [model for model in models if model]

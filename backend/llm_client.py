import json
from urllib import error, request


class OpenAICompatibleClient:
    def generate(self, base_url: str, api_key: str, model: str, prompt: str) -> str:
        url = f"{base_url.rstrip('/')}/chat/completions"
        payload = json.dumps(
            {
                "model": model,
                "messages": [{"role": "user", "content": prompt}],
            }
        ).encode()
        req = request.Request(
            url,
            data=payload,
            headers={
                "Authorization": f"Bearer {api_key}",
                "Content-Type": "application/json",
            },
            method="POST",
        )
        try:
            with request.urlopen(req, timeout=30) as response:
                body = json.loads(response.read().decode())
        except error.HTTPError as exc:
            raise RuntimeError(f"LLM request failed with HTTP {exc.code}") from exc
        except TimeoutError as exc:
            raise RuntimeError("LLM request timed out") from exc
        except error.URLError as exc:
            raise RuntimeError(f"LLM request failed: {exc.reason}") from exc
        except (json.JSONDecodeError, UnicodeDecodeError) as exc:
            raise RuntimeError("LLM response was not valid JSON") from exc

        try:
            content = body["choices"][0]["message"]["content"]
        except (KeyError, IndexError, TypeError) as exc:
            raise RuntimeError("LLM response was malformed") from exc
        if not isinstance(content, str) or not content:
            raise RuntimeError("LLM response was malformed")
        return content

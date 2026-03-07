"""Integration tests — call the real running server at localhost:6969.

Prerequisites:
- Server running: poetry run python src/run.py --reload
- Gemini client connected (valid cookies)

Run:
    conda activate linhdz && python -m pytest tests/test_integration_structured_output.py -v -s
"""

import json
import httpx
import pytest

BASE_URL = "http://localhost:6969"
TIMEOUT = 60.0  # Gemini can be slow
MODEL = "gemini-3.0-pro"  # Use a model the server actually supports


@pytest.fixture(scope="module")
def client():
    with httpx.Client(base_url=BASE_URL, timeout=TIMEOUT) as c:
        yield c


def _check_server(client: httpx.Client):
    """Skip all tests if the server is not reachable."""
    try:
        r = client.get("/v1/models")
        r.raise_for_status()
    except (httpx.ConnectError, httpx.ConnectTimeout, httpx.HTTPStatusError, OSError) as e:
        pytest.skip(f"Server not running at localhost:6969: {e}")


# =========================================================================
# Basic connectivity
# =========================================================================

class TestServerHealth:
    def test_models_endpoint(self, client):
        _check_server(client)
        r = client.get("/v1/models")
        assert r.status_code == 200
        data = r.json()
        assert data["object"] == "list"
        assert len(data["data"]) > 0


# =========================================================================
# Plain text (no structured output)
# =========================================================================

class TestPlainText:
    def test_simple_chat(self, client):
        _check_server(client)
        r = client.post("/v1/chat/completions", json={
            "model": MODEL,
            "messages": [{"role": "user", "content": "Say hello in one word."}],
        })
        assert r.status_code == 200
        data = r.json()
        assert data["choices"][0]["message"]["content"]
        assert data["choices"][0]["finish_reason"] == "stop"
        print(f"  [Plain] Response: {data['choices'][0]['message']['content'][:100]}")


# =========================================================================
# Structured output: json_object mode
# =========================================================================

class TestJsonObjectMode:
    def test_json_object_returns_valid_json(self, client):
        _check_server(client)
        r = client.post("/v1/chat/completions", json={
            "model": MODEL,
            "messages": [{"role": "user", "content": "List 3 colors with hex codes."}],
            "response_format": {"type": "json_object"},
        })
        assert r.status_code == 200
        content = r.json()["choices"][0]["message"]["content"]
        print(f"  [json_object] Raw: {content[:200]}")
        # Must parse as valid JSON
        parsed = json.loads(content)
        assert isinstance(parsed, (dict, list))


# =========================================================================
# Structured output: json_schema mode
# =========================================================================

class TestJsonSchemaMode:
    SCHEMA = {
        "type": "object",
        "properties": {
            "name": {"type": "string"},
            "year_created": {"type": "integer"},
            "paradigms": {
                "type": "array",
                "items": {"type": "string"},
            },
        },
        "required": ["name", "year_created", "paradigms"],
    }

    def test_json_schema_returns_conforming_json(self, client):
        _check_server(client)
        r = client.post("/v1/chat/completions", json={
            "model": MODEL,
            "messages": [{"role": "user", "content": "Give me info about Python programming language."}],
            "response_format": {
                "type": "json_schema",
                "json_schema": {
                    "name": "language_info",
                    "schema": self.SCHEMA,
                },
            },
        })
        assert r.status_code == 200
        content = r.json()["choices"][0]["message"]["content"]
        print(f"  [json_schema] Raw: {content[:300]}")

        parsed = json.loads(content)
        assert isinstance(parsed, dict)
        # Verify required fields exist
        assert "name" in parsed, f"Missing 'name' in {parsed}"
        assert "year_created" in parsed, f"Missing 'year_created' in {parsed}"
        assert "paradigms" in parsed, f"Missing 'paradigms' in {parsed}"
        # Type checks
        assert isinstance(parsed["name"], str)
        assert isinstance(parsed["year_created"], int)
        assert isinstance(parsed["paradigms"], list)

    def test_json_schema_complex_nested(self, client):
        _check_server(client)
        schema = {
            "type": "object",
            "properties": {
                "students": {
                    "type": "array",
                    "items": {
                        "type": "object",
                        "properties": {
                            "name": {"type": "string"},
                            "age": {"type": "integer"},
                            "grades": {
                                "type": "object",
                                "properties": {
                                    "math": {"type": "number"},
                                    "science": {"type": "number"},
                                },
                            },
                        },
                        "required": ["name", "age"],
                    },
                },
            },
            "required": ["students"],
        }
        r = client.post("/v1/chat/completions", json={
            "model": MODEL,
            "messages": [{"role": "user", "content": "Generate data for 2 fictional students with math and science grades."}],
            "response_format": {
                "type": "json_schema",
                "json_schema": {"name": "student_data", "schema": schema},
            },
        })
        assert r.status_code == 200
        content = r.json()["choices"][0]["message"]["content"]
        print(f"  [nested schema] Raw: {content[:400]}")

        parsed = json.loads(content)
        assert "students" in parsed
        assert len(parsed["students"]) >= 2
        for student in parsed["students"]:
            assert "name" in student
            assert "age" in student


# =========================================================================
# Streaming + structured output
# =========================================================================

class TestStreamingStructuredOutput:
    def test_stream_json_object(self, client):
        _check_server(client)
        with client.stream("POST", "/v1/chat/completions", json={
            "model": MODEL,
            "messages": [{"role": "user", "content": "Return a JSON with key 'status' and value 'ok'."}],
            "response_format": {"type": "json_object"},
            "stream": True,
        }) as r:
            assert r.status_code == 200
            chunks = []
            for line in r.iter_lines():
                if line.startswith("data: ") and line != "data: [DONE]":
                    chunk = json.loads(line[6:])
                    delta = chunk["choices"][0].get("delta", {})
                    if "content" in delta:
                        chunks.append(delta["content"])

            full_content = "".join(chunks)
            print(f"  [stream json_object] Full: {full_content[:200]}")
            parsed = json.loads(full_content)
            assert parsed.get("status") == "ok"


# =========================================================================
# No response_format (backward compatibility)
# =========================================================================

class TestBackwardCompatibility:
    def test_no_response_format_field(self, client):
        """Existing requests without response_format should still work."""
        _check_server(client)
        r = client.post("/v1/chat/completions", json={
            "model": MODEL,
            "messages": [{"role": "user", "content": "What is 2+2? Answer in one word."}],
        })
        assert r.status_code == 200
        content = r.json()["choices"][0]["message"]["content"]
        assert content  # non-empty
        print(f"  [no format] Response: {content[:100]}")

    def test_text_response_format(self, client):
        """Explicit type=text should work like no response_format."""
        _check_server(client)
        r = client.post("/v1/chat/completions", json={
            "model": MODEL,
            "messages": [{"role": "user", "content": "What is 2+2? Answer in one word."}],
            "response_format": {"type": "text"},
        })
        assert r.status_code == 200
        content = r.json()["choices"][0]["message"]["content"]
        assert content
        print(f"  [type=text] Response: {content[:100]}")

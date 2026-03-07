"""Tests for structured output helpers: _build_json_system_prompt, _extract_json, and schema models."""

import json
import sys
import os
import pytest

# Ensure src/ is on the path so we can import project modules
sys.path.insert(0, os.path.join(os.path.dirname(__file__), "..", "src"))

from schemas.request import ResponseFormat, JsonSchemaSpec
from app.endpoints.chat import _build_json_system_prompt, _extract_json


# =========================================================================
# ResponseFormat / JsonSchemaSpec schema models
# =========================================================================

class TestResponseFormatModel:
    """Test Pydantic models for response_format."""

    def test_default_text_format(self):
        rf = ResponseFormat()
        assert rf.type == "text"
        assert rf.json_schema is None

    def test_json_object_format(self):
        rf = ResponseFormat(type="json_object")
        assert rf.type == "json_object"

    def test_json_schema_format(self):
        rf = ResponseFormat(
            type="json_schema",
            json_schema=JsonSchemaSpec(
                name="test",
                description="A test schema",
                schema={"type": "object", "properties": {"name": {"type": "string"}}},
            ),
        )
        assert rf.type == "json_schema"
        assert rf.json_schema.name == "test"
        assert rf.json_schema.schema_["type"] == "object"

    def test_json_schema_from_openai_payload(self):
        """Simulate parsing a real OpenAI-style payload."""
        payload = {
            "type": "json_schema",
            "json_schema": {
                "name": "language_info",
                "schema": {
                    "type": "object",
                    "properties": {
                        "name": {"type": "string"},
                        "year_created": {"type": "integer"},
                    },
                    "required": ["name"],
                },
            },
        }
        rf = ResponseFormat(**payload)
        assert rf.json_schema.name == "language_info"
        assert rf.json_schema.schema_["properties"]["name"]["type"] == "string"


# =========================================================================
# _build_json_system_prompt
# =========================================================================

class TestBuildJsonSystemPrompt:
    def test_text_format_returns_empty(self):
        rf = ResponseFormat(type="text")
        assert _build_json_system_prompt(rf) == ""

    def test_json_object_prompt(self):
        rf = ResponseFormat(type="json_object")
        prompt = _build_json_system_prompt(rf)
        assert "valid JSON" in prompt
        assert "code blocks" in prompt

    def test_json_schema_prompt_includes_schema(self):
        schema = {"type": "object", "properties": {"x": {"type": "integer"}}}
        rf = ResponseFormat(
            type="json_schema",
            json_schema=JsonSchemaSpec(name="test", schema=schema),
        )
        prompt = _build_json_system_prompt(rf)
        assert '"type": "object"' in prompt
        assert "valid JSON" in prompt

    def test_json_schema_prompt_includes_description(self):
        rf = ResponseFormat(
            type="json_schema",
            json_schema=JsonSchemaSpec(
                name="test",
                description="My custom description",
                schema={"type": "object"},
            ),
        )
        prompt = _build_json_system_prompt(rf)
        assert "My custom description" in prompt


# =========================================================================
# _extract_json — core extraction + repair logic
# =========================================================================

class TestExtractJson:
    """Test JSON extraction, repair, and schema validation."""

    # --- Clean JSON passthrough ---

    def test_clean_json_object(self):
        text = '{"name": "Python", "year": 1991}'
        result = _extract_json(text)
        parsed = json.loads(result)
        assert parsed["name"] == "Python"
        assert parsed["year"] == 1991

    def test_clean_json_array(self):
        text = '[1, 2, 3]'
        result = _extract_json(text)
        assert json.loads(result) == [1, 2, 3]

    # --- Markdown fences ---

    def test_markdown_json_fence(self):
        text = '```json\n{"key": "value"}\n```'
        result = _extract_json(text)
        assert json.loads(result) == {"key": "value"}

    def test_markdown_plain_fence(self):
        text = '```\n{"key": "value"}\n```'
        result = _extract_json(text)
        assert json.loads(result) == {"key": "value"}

    def test_markdown_fence_with_surrounding_text(self):
        text = 'Here is the JSON:\n```json\n{"a": 1}\n```\nHope this helps!'
        result = _extract_json(text)
        assert json.loads(result) == {"a": 1}

    # --- json-repair: trailing commas ---

    def test_trailing_comma_in_object(self):
        text = '{"a": 1, "b": 2,}'
        result = _extract_json(text)
        parsed = json.loads(result)
        assert parsed == {"a": 1, "b": 2}

    def test_trailing_comma_in_array(self):
        text = '[1, 2, 3,]'
        result = _extract_json(text)
        assert json.loads(result) == [1, 2, 3]

    # --- json-repair: single quotes ---

    def test_single_quotes(self):
        text = "{'name': 'Python', 'year': 1991}"
        result = _extract_json(text)
        parsed = json.loads(result)
        assert parsed["name"] == "Python"

    # --- json-repair: missing quotes on keys ---

    def test_unquoted_keys(self):
        text = '{name: "Python", year: 1991}'
        result = _extract_json(text)
        parsed = json.loads(result)
        assert parsed["name"] == "Python"

    # --- json-repair: mixed issues ---

    def test_multiple_issues_combined(self):
        """Single quotes + trailing comma + unquoted key."""
        text = "{'name': 'Python', year: 1991,}"
        result = _extract_json(text)
        parsed = json.loads(result)
        assert parsed["name"] == "Python"
        assert parsed["year"] == 1991

    # --- LLM output with explanation text around JSON ---

    def test_json_with_preamble(self):
        text = 'Sure! Here is the result:\n\n{"answer": 42}\n\nLet me know if you need more.'
        result = _extract_json(text)
        parsed = json.loads(result)
        assert parsed["answer"] == 42

    def test_json_in_markdown_with_preamble(self):
        text = (
            "Based on your request, here is the structured data:\n\n"
            "```json\n"
            '{\n  "name": "test",\n  "value": 123\n}\n'
            "```\n\n"
            "Feel free to ask follow-up questions."
        )
        result = _extract_json(text)
        parsed = json.loads(result)
        assert parsed["name"] == "test"
        assert parsed["value"] == 123

    # --- Nested JSON ---

    def test_nested_json(self):
        text = '{"user": {"name": "Alice", "scores": [10, 20, 30]}}'
        result = _extract_json(text)
        parsed = json.loads(result)
        assert parsed["user"]["name"] == "Alice"
        assert parsed["user"]["scores"] == [10, 20, 30]

    # --- Unicode ---

    def test_unicode_content(self):
        text = '{"greeting": "Xin chào thế giới", "emoji": "🎉"}'
        result = _extract_json(text)
        parsed = json.loads(result)
        assert parsed["greeting"] == "Xin chào thế giới"

    # --- Schema validation ---

    def test_valid_schema_passes(self):
        schema = {
            "type": "object",
            "properties": {"name": {"type": "string"}, "age": {"type": "integer"}},
            "required": ["name"],
        }
        text = '{"name": "Alice", "age": 30}'
        result = _extract_json(text, schema=schema)
        parsed = json.loads(result)
        assert parsed["name"] == "Alice"

    def test_invalid_schema_still_returns_json(self):
        """Schema validation is best-effort — invalid JSON is still returned."""
        schema = {
            "type": "object",
            "properties": {"name": {"type": "string"}},
            "required": ["name"],
        }
        text = '{"age": 30}'  # missing required "name"
        result = _extract_json(text, schema=schema)
        parsed = json.loads(result)
        assert parsed["age"] == 30  # still returned, just logged a warning

    def test_schema_with_repaired_json(self):
        """Repair broken JSON, then validate against schema."""
        schema = {
            "type": "object",
            "properties": {"x": {"type": "integer"}, "y": {"type": "integer"}},
        }
        text = "{'x': 10, 'y': 20,}"  # single quotes + trailing comma
        result = _extract_json(text, schema=schema)
        parsed = json.loads(result)
        assert parsed == {"x": 10, "y": 20}

    # --- Edge cases ---

    def test_empty_string(self):
        """Empty input should not crash."""
        result = _extract_json("")
        assert isinstance(result, str)

    def test_plain_text_no_json(self):
        """Non-JSON text should be returned without crash."""
        text = "I don't have any JSON for you."
        result = _extract_json(text)
        assert isinstance(result, str)

    def test_boolean_json(self):
        text = "true"
        result = _extract_json(text)
        assert json.loads(result) is True

    def test_null_json(self):
        text = "null"
        result = _extract_json(text)
        assert json.loads(result) is None

    def test_number_json(self):
        text = "42"
        result = _extract_json(text)
        assert json.loads(result) == 42

# src/schemas/request.py
from enum import Enum
from typing import Any, List, Optional, Union
from pydantic import BaseModel, ConfigDict, Field


# ---------------------------------------------------------------------------
# Multimodal content part schemas (OpenAI vision format)
# ---------------------------------------------------------------------------

class ImageUrlDetail(BaseModel):
    """Inner object for image_url content parts."""
    url: str
    detail: Optional[str] = "auto"


class ContentPart(BaseModel):
    """A single part of a multimodal message content array."""
    type: str  # "text" | "image_url"
    text: Optional[str] = None
    image_url: Optional[ImageUrlDetail] = None


# ---------------------------------------------------------------------------
# Gemini model enum
# ---------------------------------------------------------------------------

class GeminiModels(str, Enum):
    """
    Available Gemini models (gemini-webapi >= 1.19.2).
    """

    # Gemini 3.0 Series
    PRO = "gemini-3.0-pro"
    FLASH = "gemini-3.0-flash"
    FLASH_THINKING = "gemini-3.0-flash-thinking"


class GeminiRequest(BaseModel):
    message: str
    model: GeminiModels = Field(default=GeminiModels.FLASH, description="Model to use for Gemini.")
    files: Optional[List[str]] = []

class JsonSchemaSpec(BaseModel):
    """Schema definition for structured JSON output.

    Example::

        {
          "name": "language_info",
          "description": "Information about a programming language",
          "schema": {
            "type": "object",
            "properties": {
              "name": {"type": "string"},
              "year_created": {"type": "integer"},
              "paradigms": {"type": "array", "items": {"type": "string"}}
            },
            "required": ["name", "year_created"]
          }
        }
    """
    name: str = Field(default="response", description="A name for the schema, used for identification.")
    description: Optional[str] = Field(default=None, description="Optional description of what the schema represents.")
    schema_: Optional[dict] = Field(default=None, alias="schema", description="The JSON Schema object that the output must conform to.")
    strict: Optional[bool] = Field(default=None, description="If true, stricter validation is applied (best-effort).")

    model_config = ConfigDict(populate_by_name=True)


class ResponseFormat(BaseModel):
    """OpenAI-compatible response_format parameter for structured output.

    Supported types:

    - ``text`` (default): No constraint, free-form text response.
    - ``json_object``: Forces the model to return valid JSON.
    - ``json_schema``: Forces the model to return JSON conforming to the provided schema.

    **json_object example:**

    ``{"type": "json_object"}``

    **json_schema example:**

    ::

        {
          "type": "json_schema",
          "json_schema": {
            "name": "math_result",
            "schema": {
              "type": "object",
              "properties": {
                "answer": {"type": "number"},
                "explanation": {"type": "string"}
              },
              "required": ["answer"]
            }
          }
        }
    """
    type: str = Field(default="text", description="Output format type: 'text', 'json_object', or 'json_schema'.")
    json_schema: Optional[JsonSchemaSpec] = Field(default=None, description="Required when type is 'json_schema'. Defines the JSON Schema the output must conform to.")


class OpenAIChatRequest(BaseModel):
    """OpenAI-compatible chat completion request.

    Supports multimodal content (text + images/files), streaming, model aliases,
    and structured output via ``response_format``.
    """
    messages: List[dict] = Field(..., description="List of message objects with 'role' and 'content' fields.")
    model: Optional[str] = Field(default=None, description="Model name. Accepts OpenAI-style aliases (e.g. 'gemini-pro', 'gemini-2.5-flash') which are auto-mapped to the closest supported Gemini model.")
    stream: Optional[bool] = Field(default=False, description="If true, response is streamed as SSE events.")
    response_format: Optional[ResponseFormat] = Field(
        default=None,
        description="Structured output format. Use 'json_object' for free-form JSON or 'json_schema' with a schema definition.",
    )

class Part(BaseModel):
    text: str

class Content(BaseModel):
    parts: List[Part]

class GoogleGenerativeRequest(BaseModel):
    contents: List[Content]

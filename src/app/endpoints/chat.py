# src/app/endpoints/chat.py
import json
import re
import time
from pathlib import Path
from typing import List, Optional, Tuple

import json_repair
import jsonschema

from fastapi import APIRouter, HTTPException
from fastapi.responses import StreamingResponse

from app.config import CONFIG
from app.logger import logger
from app.services.gemini_client import GeminiClientNotInitializedError, get_gemini_client
from app.services.telegram_notifier import TelegramNotifier
from app.services.session_manager import get_translate_session_manager
from app.utils.image_utils import (
    cleanup_temp_files,
    decode_base64_to_tempfile,
    download_to_tempfile,
    get_temp_dir,
    serialize_response_images,
)
from schemas.request import GeminiModels, GeminiRequest, OpenAIChatRequest, ResponseFormat

router = APIRouter()


# ---------------------------------------------------------------------------
# Structured output helpers
# ---------------------------------------------------------------------------

def _build_json_system_prompt(response_format: ResponseFormat) -> str:
    """Build a system instruction that forces the model to return valid JSON."""
    if response_format.type == "json_object":
        return (
            "You MUST respond with valid JSON only. "
            "Do not include any text, explanation, or markdown formatting outside the JSON. "
            "Do not wrap the JSON in ```json``` code blocks. "
            "Output raw JSON directly."
        )

    if response_format.type == "json_schema" and response_format.json_schema:
        schema = response_format.json_schema
        schema_json = json.dumps(schema.schema_ or {}, ensure_ascii=False)
        desc = f" Description: {schema.description}" if schema.description else ""
        return (
            f"You MUST respond with valid JSON that conforms to the following JSON schema.{desc}\n"
            f"JSON Schema:\n{schema_json}\n\n"
            "Do not include any text, explanation, or markdown formatting outside the JSON. "
            "Do not wrap the JSON in ```json``` code blocks. "
            "Output raw JSON directly."
        )

    return ""


def _extract_json(text: str, schema: Optional[dict] = None) -> str:
    """Extract and repair JSON from LLM output using json-repair.

    Steps:
    1. Strip markdown code fences if present
    2. Use json_repair to fix common LLM JSON mistakes
       (trailing commas, single quotes, missing quotes, etc.)
    3. Validate against JSON Schema if provided

    Returns the cleaned JSON string. Raises ValueError on validation failure.
    """
    # Strip markdown code fences (```json ... ``` or ``` ... ```)
    m = re.search(r"```(?:json)?\s*\n?(.*?)\n?```", text, re.DOTALL)
    if m:
        text = m.group(1).strip()

    # Use json_repair to parse and fix broken JSON
    repaired = json_repair.repair_json(text, return_objects=True)

    # json_repair returns the parsed object; serialize back to clean JSON string
    if isinstance(repaired, (dict, list)):
        # Validate against schema if provided
        if schema:
            try:
                jsonschema.validate(instance=repaired, schema=schema)
            except jsonschema.ValidationError as e:
                logger.warning(f"Structured output schema validation failed: {e.message}")
                # Still return the JSON — validation is best-effort since LLM output
                # can't be perfectly constrained
        return json.dumps(repaired, ensure_ascii=False)

    # If repair returned a scalar or string, return as-is
    return json.dumps(repaired, ensure_ascii=False) if repaired is not None else text


# ---------------------------------------------------------------------------
# Model resolution — map any string to a valid GeminiModels value
# ---------------------------------------------------------------------------

# Explicit aliases: covers Home Assistant / OpenAI-style names and legacy names
_MODEL_ALIASES: dict[str, GeminiModels] = {
    # gemini-webapi canonical names (pass-through)
    "gemini-3.0-pro":            GeminiModels.PRO,
    "gemini-3.0-flash":          GeminiModels.FLASH,
    "gemini-3.0-flash-thinking": GeminiModels.FLASH_THINKING,
    # Home Assistant / common variants
    "gemini-pro":                GeminiModels.PRO,
    "gemini-ultra":              GeminiModels.PRO,
    "gemini-flash":              GeminiModels.FLASH,
    "gemini-1.0-pro":            GeminiModels.PRO,
    "gemini-1.5-pro":            GeminiModels.PRO,
    "gemini-1.5-pro-latest":     GeminiModels.PRO,
    "gemini-1.5-flash":          GeminiModels.FLASH,
    "gemini-1.5-flash-latest":   GeminiModels.FLASH,
    "gemini-2.0-flash":          GeminiModels.FLASH,
    "gemini-2.0-flash-exp":      GeminiModels.FLASH,
    "gemini-2.0-pro":            GeminiModels.PRO,
    "gemini-2.5-pro":            GeminiModels.PRO,
    "gemini-2.5-flash":          GeminiModels.FLASH,
    "gemini-3-pro":              GeminiModels.PRO,
    "gemini-3-flash":            GeminiModels.FLASH,
    "gemini-3-flash-thinking":   GeminiModels.FLASH_THINKING,
}


def _resolve_model(model_str: Optional[str]) -> GeminiModels:
    """
    Resolve any model string to a supported GeminiModels value.

    Lookup priority:
    1. Exact match in alias table (case-insensitive)
    2. Substring heuristics: "thinking" → FLASH_THINKING, "pro" → PRO, "flash" → FLASH
    3. Default: FLASH

    Logs a warning when an unknown name is mapped so the operator can see what HA is sending.
    """
    if not model_str:
        return GeminiModels.FLASH

    lower = model_str.strip().lower()

    # Exact alias match
    if lower in _MODEL_ALIASES:
        return _MODEL_ALIASES[lower]

    # Substring heuristics (handles "gemini-3-pro-image-preview" etc.)
    if "thinking" in lower:
        resolved = GeminiModels.FLASH_THINKING
    elif "pro" in lower:
        resolved = GeminiModels.PRO
    elif "flash" in lower:
        resolved = GeminiModels.FLASH
    else:
        resolved = GeminiModels.FLASH

    logger.warning(
        f"Unknown model '{model_str}' → mapped to '{resolved.value}'. "
        f"Add an explicit alias in _MODEL_ALIASES if needed."
    )
    return resolved


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------

def _get_cookies(gemini_client) -> dict:
    """Extract session cookies from the underlying Gemini web client."""
    try:
        return dict(gemini_client.client.cookies)
    except Exception:
        return {}


async def _extract_multimodal_content(content) -> Tuple[str, List[Path]]:
    """
    Parse a message ``content`` field that may be:
    - a plain string
    - a list of content part dicts

    Supports both Chat Completions and Responses API part types:
    - Chat Completions: ``{"type": "text"|"image_url", ...}``
    - Responses API:    ``{"type": "input_text"|"input_image", ...}``

    For ``image_url``/``input_image``, the URL value may be:
    - Chat Completions: ``{"image_url": {"url": "data:..."}}``)
    - Responses API:    ``{"image_url": "data:..."}``  (direct string)

    Returns ``(text_prompt, temp_file_paths)``.
    Temp files are created for base64 data URIs and remote URL images; the caller
    is responsible for cleaning them up after use.
    """
    if isinstance(content, str):
        return content, []

    if not isinstance(content, list):
        return str(content) if content else "", []

    text_parts: List[str] = []
    file_paths: List[Path] = []

    for part in content:
        if not isinstance(part, dict):
            continue

        part_type = part.get("type", "")

        # Text parts — Chat Completions ("text") and Responses API ("input_text")
        if part_type in ("text", "input_text"):
            txt = part.get("text", "")
            if txt:
                text_parts.append(txt)

        # Image parts — Chat Completions ("image_url") and Responses API ("input_image")
        elif part_type in ("image_url", "input_image"):
            img_url_obj = part.get("image_url", {})
            # Chat Completions: image_url is {"url": "...", "detail": "..."}
            # Responses API:    image_url is a direct string "data:..." or "https://..."
            url: str = img_url_obj.get("url", "") if isinstance(img_url_obj, dict) else str(img_url_obj)

            if not url:
                continue

            if url.startswith("data:"):
                # base64 data URI
                try:
                    temp_path = decode_base64_to_tempfile(url)
                    file_paths.append(temp_path)
                except ValueError as exc:
                    logger.warning(f"Skipping invalid base64 image: {exc}")

            elif url.startswith("file://"):
                # Reference to a local file. Supports:
                #   1. file://<file_id>         → look up inside temp dir
                #   2. file:///absolute/path    → any local file path
                raw_path = url[len("file://"):]

                if raw_path.startswith("/"):
                    # Absolute path — resolve and check existence
                    candidate = Path(raw_path).resolve()
                    if candidate.exists() and candidate.is_file():
                        file_paths.append(candidate)
                    else:
                        logger.warning(f"Local file not found: {candidate}")
                elif "/" not in raw_path and "\\" not in raw_path and ".." not in raw_path:
                    # Plain file_id from /v1/files upload
                    candidate = get_temp_dir() / raw_path
                    if candidate.exists():
                        file_paths.append(candidate)
                    else:
                        logger.warning(f"File not found for file_id: {raw_path}")
                else:
                    logger.warning(f"Invalid file_id in URL: {url}")

            elif url.startswith("http://") or url.startswith("https://"):
                # Remote image URL — download to temp file
                temp_path = await download_to_tempfile(url)
                if temp_path:
                    file_paths.append(temp_path)

    return " ".join(text_parts), file_paths


# ---------------------------------------------------------------------------
# Model listing
# ---------------------------------------------------------------------------

@router.get("/v1/models")
async def list_models():
    """List available models in OpenAI-compatible format."""
    now = int(time.time())
    models = [
        {
            "id": m.value,
            "object": "model",
            "created": now,
            "owned_by": "google",
        }
        for m in GeminiModels
    ]
    return {"object": "list", "data": models}


# ---------------------------------------------------------------------------
# Translation endpoint
# ---------------------------------------------------------------------------

@router.post("/translate")
async def translate_chat(request: GeminiRequest):
    try:
        gemini_client = get_gemini_client()
    except GeminiClientNotInitializedError as e:
        raise HTTPException(status_code=503, detail=str(e))

    session_manager = get_translate_session_manager()
    if not session_manager:
        raise HTTPException(status_code=503, detail="Session manager is not initialized.")
    try:
        response = await session_manager.get_response(request.model, request.message, request.files)
        return {"response": response.text}
    except Exception as e:
        logger.error(f"Error in /translate endpoint: {e}", exc_info=True)
        raise HTTPException(status_code=500, detail=f"Error during translation: {str(e)}")


# ---------------------------------------------------------------------------
# OpenAI-compatible streaming helpers
# ---------------------------------------------------------------------------

def _to_openai_format(response_text: str, model: str, images: list, stream: bool = False) -> dict:
    """Build an OpenAI-compatible chat completion response dict."""
    content = response_text
    # Append image references as markdown if present (keeps text content useful)
    if images:
        md_links = "\n".join(
            f"![{img['title']}]({img['url']})" for img in images
        )
        content = f"{response_text}\n\n{md_links}".strip()

    result = {
        "id": f"chatcmpl-{int(time.time())}",
        "object": "chat.completion.chunk" if stream else "chat.completion",
        "created": int(time.time()),
        "model": model,
        "choices": [
            {
                "index": 0,
                "message": {
                    "role": "assistant",
                    "content": content,
                },
                "finish_reason": "stop",
            }
        ],
        "usage": {
            "prompt_tokens": 0,
            "completion_tokens": 0,
            "total_tokens": 0,
        },
    }
    # Attach raw images as a top-level extension field
    if images:
        result["images"] = images
    return result


async def _stream_response(response_text: str, model: str, images: list):
    """Yield SSE chunks in OpenAI streaming format."""
    completion_id = f"chatcmpl-{int(time.time())}"
    created = int(time.time())

    content = response_text
    if images:
        md_links = "\n".join(f"![{img['title']}]({img['url']})" for img in images)
        content = f"{response_text}\n\n{md_links}".strip()

    first_chunk = {
        "id": completion_id,
        "object": "chat.completion.chunk",
        "created": created,
        "model": model,
        "choices": [{"index": 0, "delta": {"role": "assistant", "content": ""}, "finish_reason": None}],
    }
    yield f"data: {json.dumps(first_chunk)}\n\n"

    content_chunk = {
        "id": completion_id,
        "object": "chat.completion.chunk",
        "created": created,
        "model": model,
        "choices": [{"index": 0, "delta": {"content": content}, "finish_reason": None}],
    }
    if images:
        content_chunk["images"] = images
    yield f"data: {json.dumps(content_chunk)}\n\n"

    final_chunk = {
        "id": completion_id,
        "object": "chat.completion.chunk",
        "created": created,
        "model": model,
        "choices": [{"index": 0, "delta": {}, "finish_reason": "stop"}],
    }
    yield f"data: {json.dumps(final_chunk)}\n\n"
    yield "data: [DONE]\n\n"


# ---------------------------------------------------------------------------
# OpenAI-compatible chat completions
# ---------------------------------------------------------------------------

@router.post("/v1/chat/completions")
async def chat_completions(request: OpenAIChatRequest):
    """
    OpenAI-compatible chat completion endpoint.

    **Features:**
    - Plain text and multimodal messages (images, PDFs)
    - ``image_url`` content parts: base64 data URIs, HTTPS URLs, or ``file://`` references
    - Streaming via SSE (``stream: true``)
    - Structured output via ``response_format``
    - ``thoughts`` field in response (thinking models)
    - ``images`` field in response (web/generated images)

    **Structured Output Examples:**

    *JSON Object mode* — returns valid JSON (free-form):
    ```json
    {
      "model": "gemini-3.0-pro",
      "messages": [{"role": "user", "content": "List 3 colors as JSON"}],
      "response_format": {"type": "json_object"}
    }
    ```

    *JSON Schema mode* — returns JSON conforming to a schema:
    ```json
    {
      "model": "gemini-3.0-pro",
      "messages": [{"role": "user", "content": "Info about Python"}],
      "response_format": {
        "type": "json_schema",
        "json_schema": {
          "name": "language_info",
          "schema": {
            "type": "object",
            "properties": {
              "name": {"type": "string"},
              "year": {"type": "integer"}
            },
            "required": ["name", "year"]
          }
        }
      }
    }
    ```

    Broken JSON from the model is auto-repaired (trailing commas, single quotes, etc.).
    Schema validation is best-effort — the response is always returned.
    """
    try:
        gemini_client = get_gemini_client()
    except GeminiClientNotInitializedError as e:
        raise HTTPException(status_code=503, detail=str(e))

    is_stream = bool(request.stream)

    if not request.messages:
        raise HTTPException(status_code=400, detail="No messages provided.")

    # Resolve model string → GeminiModels (handles HA aliases like "gemini-3-pro-image-preview")
    gemini_model = _resolve_model(request.model)
    model_value = gemini_model.value

    # Parse all messages — collect text parts and any image file paths
    conversation_parts: List[str] = []
    all_file_paths: List[Path] = []
    # Track which paths are temp files that should be cleaned up
    temp_file_paths: List[Path] = []

    for msg in request.messages:
        role = msg.get("role", "user")
        raw_content = msg.get("content", "")

        text, file_paths = await _extract_multimodal_content(raw_content)

        # Mark newly created temp files for cleanup
        for fp in file_paths:
            if str(fp).startswith(str(get_temp_dir())):
                temp_file_paths.append(fp)
        all_file_paths.extend(file_paths)

        if not text:
            continue

        if role == "system":
            conversation_parts.append(f"System: {text}")
        elif role == "user":
            conversation_parts.append(f"User: {text}")
        elif role == "assistant":
            conversation_parts.append(f"Assistant: {text}")

    if not conversation_parts:
        raise HTTPException(status_code=400, detail="No valid messages found.")

    final_prompt = "\n\n".join(conversation_parts)
    files_arg = all_file_paths if all_file_paths else None

    # Structured output: inject JSON instructions into the prompt
    json_mode = False
    json_schema_dict = None
    if request.response_format and request.response_format.type in ("json_object", "json_schema"):
        json_mode = True
        json_instruction = _build_json_system_prompt(request.response_format)
        final_prompt = f"System: {json_instruction}\n\n{final_prompt}"
        if (request.response_format.type == "json_schema"
                and request.response_format.json_schema
                and request.response_format.json_schema.schema_):
            json_schema_dict = request.response_format.json_schema.schema_

    try:
        response = await gemini_client.generate_content(
            message=final_prompt,
            model=model_value,
            files=files_arg,
        )

        response_text = response.text
        if json_mode:
            response_text = _extract_json(response_text, schema=json_schema_dict)

        images = await serialize_response_images(
            response, gemini_cookies=_get_cookies(gemini_client)
        )

        if is_stream:
            return StreamingResponse(
                _stream_response(response_text, model_value, images),
                media_type="text/event-stream",
                headers={"Cache-Control": "no-cache", "X-Accel-Buffering": "no"},
            )
        return _to_openai_format(response_text, model_value, images, is_stream)

    except Exception as e:
        err_str = str(e)
        err_lower = err_str.lower()
        notifier = TelegramNotifier.get_instance()
        if "auth" in err_lower or "cookie" in err_lower:
            logger.error(f"[chat/completions] Auth error: {e}")
            await notifier.notify_error("auth", "Authentication failed", "/v1/chat/completions", err_str)
            raise HTTPException(status_code=401, detail=f"Gemini authentication failed: {err_str}")
        elif "zombie" in err_lower or "parse" in err_lower or "stalled" in err_lower:
            logger.error(f"[chat/completions] Stream error after retries (model={model_value}): {e}")
            await notifier.notify_error("503", "Stream temporarily unavailable", "/v1/chat/completions", err_str)
            raise HTTPException(status_code=503, detail="Gemini stream temporarily unavailable — please retry")
        else:
            logger.error(f"[chat/completions] Unexpected error (model={model_value}): {e}", exc_info=True)
            await notifier.notify_error("500", "Unexpected error", "/v1/chat/completions", err_str)
            raise HTTPException(status_code=500, detail=f"Error processing chat completion: {err_str}")

    finally:
        # Clean up temp files created from base64/URL image inputs
        cleanup_temp_files(temp_file_paths)

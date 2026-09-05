"""Vision tool — image analysis via multimodal model."""

from __future__ import annotations

import base64
import json
import mimetypes
from pathlib import Path
from typing import Optional

from gyrfalcon.tools import registry
from gyrfalcon.gyrfalcon_logging import get_logger

logger = get_logger("tools.vision")


def _is_url(path_or_url: str) -> bool:
    """Check if the input is a URL."""
    logger.debug("Beginning of _is_url")
    return path_or_url.startswith(("http://", "https://", "data:"))


def _encode_local_image(file_path: str) -> tuple[str, str]:
    """Read and base64-encode a local image file. Returns (base64_data, media_type)."""
    logger.debug("Beginning of _encode_local_image")
    path = Path(file_path).expanduser().resolve()
    if not path.exists():
        raise FileNotFoundError(f"Image file not found: {file_path}")
    if not path.is_file():
        raise ValueError(f"Not a file: {file_path}")

    mime_type, _ = mimetypes.guess_type(str(path))
    if not mime_type or not mime_type.startswith("image/"):
        mime_type = "image/png"  # Default fallback

    data = path.read_bytes()
    b64 = base64.b64encode(data).decode("utf-8")
    return b64, mime_type


def _build_image_content(image_path_or_url: str) -> dict:
    """Build the image content block for a vision message."""
    logger.debug("Beginning of _build_image_content")
    if _is_url(image_path_or_url):
        return {
            "type": "image_url",
            "image_url": {"url": image_path_or_url},
        }
    else:
        b64_data, media_type = _encode_local_image(image_path_or_url)
        data_url = f"data:{media_type};base64,{b64_data}"
        return {
            "type": "image_url",
            "image_url": {"url": data_url},
        }


async def analyze_image(args: dict, **kwargs) -> str:
    """Analyze an image using the model's vision capabilities."""
    image_path_or_url = args.get("image_path_or_url", "") or args.get("image", "")
    question = args.get("question", "Describe this image in detail.")

    if not image_path_or_url:
        return json.dumps({"error": "No image_path_or_url provided"})

    try:
        image_content = _build_image_content(image_path_or_url)
    except FileNotFoundError as e:
        return json.dumps({"error": str(e)})
    except ValueError as e:
        return json.dumps({"error": str(e)})
    except Exception as e:
        return json.dumps({"error": f"Failed to process image: {str(e)}"})

    # Build a vision message for the model
    messages = [
        {
            "role": "user",
            "content": [
                {"type": "text", "text": question},
                image_content,
            ],
        }
    ]

    # Use the agent context's LLM client if available
    agent_context = kwargs.get("agent_context")
    if agent_context and hasattr(agent_context, "llm_client"):
        try:
            response = await agent_context.llm_client.chat(
                messages=messages,
                model=kwargs.get("model"),
            )
            answer = response.get("content", "") if isinstance(response, dict) else str(response)
            return json.dumps({
                "analysis": answer,
                "image": image_path_or_url,
                "question": question,
            })
        except Exception as e:
            logger.error(f"Vision LLM call failed: {e}", exc_info=True)
            return json.dumps({"error": f"Vision analysis failed: {str(e)}"})

    # Fallback: try using httpx with OpenAI-compatible API
    try:
        from gyrfalcon.net import httpx_request
        import os

        api_key = os.environ.get("OPENAI_API_KEY") or os.environ.get("ANTHROPIC_API_KEY")
        api_base = os.environ.get("OPENAI_API_BASE", "https://api.openai.com/v1")

        if not api_key:
            return json.dumps({
                "error": "No LLM client available and no API key found. "
                "Set OPENAI_API_KEY or pass agent_context with llm_client."
            })

        response = httpx_request(
            "POST",
            f"{api_base}/chat/completions",
            headers={"Authorization": f"Bearer {api_key}", "Content-Type": "application/json"},
            json={
                "model": os.environ.get("VISION_MODEL", "gpt-4o"),
                "messages": messages,
                "max_tokens": 1024,
            },
            timeout=60,
        )
        response.raise_for_status()
        data = response.json()
        answer = data["choices"][0]["message"]["content"]

        return json.dumps({
            "analysis": answer,
            "image": image_path_or_url,
            "question": question,
        })
    except Exception as e:
        logger.error(f"Vision fallback failed: {e}", exc_info=True)
        return json.dumps({"error": f"Vision analysis failed: {str(e)}"})


# Register tool
registry.register(
    name="analyze_image",
    toolset="vision",
    schema={
        "name": "analyze_image",
        "description": "Analyze an image using vision capabilities. Supports local file paths and URLs. Ask questions about the image content.",
        "parameters": {
            "type": "object",
            "properties": {
                "image_path_or_url": {
                    "type": "string",
                    "description": "Path to a local image file or URL of an image to analyze",
                },
                "question": {
                    "type": "string",
                    "description": "Question to ask about the image",
                    "default": "Describe this image in detail.",
                },
            },
            "required": ["image_path_or_url"],
        },
    },
    handler=analyze_image,
    is_async=True,
    emoji="👁️",
)

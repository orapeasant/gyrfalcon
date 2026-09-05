"""AWS Bedrock provider."""

from __future__ import annotations

import json
import os
from typing import Any, Optional

from gyrfalcon.gyrfalcon_logging import get_logger
from gyrfalcon.providers import ProviderProfile, register_provider

logger = get_logger("providers.bedrock")


class BedrockProvider(ProviderProfile):
    """AWS Bedrock inference provider."""

    def __init__(self):
        super().__init__(
            name="bedrock",
            api_mode="bedrock_converse",
            aliases=["aws-bedrock", "aws"],
            display_name="AWS Bedrock",
            env_vars=["AWS_ACCESS_KEY_ID", "AWS_SECRET_ACCESS_KEY"],
            base_url="",
            auth_type="aws_sdk",
            fallback_models=[
                "anthropic.claude-3-5-sonnet-20241022-v2:0",
                "anthropic.claude-3-5-haiku-20241022-v1:0",
                "amazon.nova-pro-v1:0",
            ],
            hostname="bedrock-runtime",
            default_model="anthropic.claude-3-5-sonnet-20241022-v2:0",
            default_max_tokens=4096,
        )

    def fetch_models(self) -> list[str]:
        logger.debug("Beginning of fetch_models")
        try:
            import boto3
            client = boto3.client("bedrock", region_name=os.environ.get("AWS_DEFAULT_REGION", "us-east-1"))
            response = client.list_foundation_models()
            return [m["modelId"] for m in response.get("modelSummaries", [])
                    if "TEXT" in m.get("outputModalities", [])]
        except Exception as e:
            logger.warning(f"Failed to list Bedrock models: {e}")
            return self.fallback_models


def create_bedrock_client(region: str | None = None):
    """Create a Bedrock Runtime client."""
    logger.debug("Beginning of create_bedrock_client")
    import boto3
    region = region or os.environ.get("AWS_DEFAULT_REGION", "us-east-1")
    return boto3.client("bedrock-runtime", region_name=region)


def bedrock_converse(
    model_id: str,
    messages: list[dict],
    system: str = "",
    tools: list[dict] | None = None,
    max_tokens: int = 4096,
    temperature: float | None = None,
    region: str | None = None,
) -> dict:
    """Make a Bedrock Converse API call."""
    logger.debug("Beginning of bedrock_converse")
    client = create_bedrock_client(region)

    # Convert messages to Bedrock format
    bedrock_messages = []
    for msg in messages:
        role = msg.get("role", "user")
        content = msg.get("content", "")

        if role == "tool":
            bedrock_messages.append({
                "role": "user",
                "content": [{
                    "toolResult": {
                        "toolUseId": msg.get("tool_call_id", ""),
                        "content": [{"text": content}],
                    }
                }],
            })
        elif role == "assistant" and msg.get("tool_calls"):
            content_blocks = []
            if content:
                content_blocks.append({"text": content})
            for tc in msg["tool_calls"]:
                args = tc["function"]["arguments"]
                if isinstance(args, str):
                    args = json.loads(args)
                content_blocks.append({
                    "toolUse": {
                        "toolUseId": tc["id"],
                        "name": tc["function"]["name"],
                        "input": args,
                    }
                })
            bedrock_messages.append({"role": "assistant", "content": content_blocks})
        else:
            if role == "system":
                continue  # System handled separately
            bedrock_messages.append({
                "role": role,
                "content": [{"text": content}] if isinstance(content, str) else content,
            })

    kwargs: dict[str, Any] = {
        "modelId": model_id,
        "messages": bedrock_messages,
        "inferenceConfig": {"maxTokens": max_tokens},
    }

    if system:
        kwargs["system"] = [{"text": system}]

    if temperature is not None:
        kwargs["inferenceConfig"]["temperature"] = temperature

    if tools:
        # Convert OpenAI tool format to Bedrock format
        bedrock_tools = []
        for tool in tools:
            if tool.get("type") == "function":
                fn = tool["function"]
                bedrock_tools.append({
                    "toolSpec": {
                        "name": fn["name"],
                        "description": fn.get("description", ""),
                        "inputSchema": {"json": fn.get("parameters", {})},
                    }
                })
        if bedrock_tools:
            kwargs["toolConfig"] = {"tools": bedrock_tools}

    response = client.converse(**kwargs)
    return response


def parse_bedrock_response(response: dict) -> dict:
    """Parse Bedrock Converse response into standard format."""
    logger.debug("Beginning of parse_bedrock_response")
    output = response.get("output", {})
    message = output.get("message", {})
    content_blocks = message.get("content", [])

    text_parts = []
    tool_calls = []

    for block in content_blocks:
        if "text" in block:
            text_parts.append(block["text"])
        elif "toolUse" in block:
            tu = block["toolUse"]
            tool_calls.append({
                "id": tu["toolUseId"],
                "type": "function",
                "function": {
                    "name": tu["name"],
                    "arguments": json.dumps(tu["input"]),
                },
            })

    result = {
        "content": "\n".join(text_parts) if text_parts else None,
        "tool_calls": tool_calls if tool_calls else None,
        "stop_reason": response.get("stopReason"),
        "usage": response.get("usage", {}),
    }
    return result


# Register provider
_bedrock_provider = BedrockProvider()
register_provider(_bedrock_provider)

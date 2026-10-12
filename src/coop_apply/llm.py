"""Thin wrapper around the Anthropic SDK for structured-output calls."""

from __future__ import annotations

from typing import Any, TypeVar

import anthropic
from pydantic import BaseModel, ValidationError

DEFAULT_MODEL = "claude-opus-5-5"
# Server-side refusal fallback: if the model declines, the API re-runs the request on
# Anthropic's recommended fallback model instead of returning an empty refusal.
FALLBACK_BETA = "server-side-fallback-2026-07-01"

T = TypeVar("T", bound=BaseModel)


class LLMError(RuntimeError):
    pass


class LLM:
    def __init__(self, model: str = DEFAULT_MODEL, client: anthropic.Anthropic | None = None):
        self.model = model
        # Credentials come from ANTHROPIC_API_KEY (or an `ant auth login` profile).
        self.client = client or anthropic.Anthropic()

    def structured(
        self,
        *,
        system: str,
        content: str | list[dict[str, Any]],
        schema: type[T],
        effort: str = "medium",
        cache_system: bool = False,
        max_tokens: int = 16000,
    ) -> T:
        system_block: dict[str, Any] = {"type": "text", "text": system}
        if cache_system:
            # The resume profile lives in the system prompt and repeats across every job
            # we score, so cache it.
            system_block["cache_control"] = {"type": "ephemeral"}
        try:
            response = self.client.beta.messages.parse(
                model=self.model,
                max_tokens=max_tokens,
                system=[system_block],
                messages=[{"role": "user", "content": content}],
                output_format=schema,
                output_config={"effort": effort},
                betas=[FALLBACK_BETA],
                fallbacks="default",
            )
        except ValidationError as e:
            # The SDK parses before we can look at stop_reason; a refusal or a reply cut
            # off at max_tokens shows up here as invalid/partial JSON.
            raise LLMError("Claude's reply didn't match the expected format (refused or cut off).") from e
        except anthropic.AuthenticationError as e:
            raise LLMError(
                "Anthropic rejected the API key (invalid or deleted). Paste a fresh key in Setup -> step 1."
            ) from e
        except anthropic.RateLimitError as e:
            raise LLMError(f"Rate limited by the Anthropic API: {e.message}") from e
        except anthropic.APIStatusError as e:
            raise LLMError(f"Anthropic API error {e.status_code}: {e.message}") from e
        except anthropic.APIConnectionError as e:
            raise LLMError("Could not reach the Anthropic API.") from e

        if response.stop_reason == "refusal":
            raise LLMError("Claude declined this request.")
        if response.stop_reason == "max_tokens":
            raise LLMError("Response was cut off (max_tokens).")
        if response.parsed_output is None:
            raise LLMError("Claude returned no structured output.")
        return response.parsed_output

    def research(self, *, system: str, prompt: str, max_searches: int = 25, effort: str = "medium",
                 max_continuations: int = 5) -> str:
        """Let Claude search the web (server-side web search) and return its written findings."""
        messages: list[dict[str, Any]] = [{"role": "user", "content": prompt}]
        tools = [{"type": "web_search_20260209", "name": "web_search", "max_uses": max_searches}]
        texts: list[str] = []
        try:
            for _ in range(max_continuations + 1):
                with self.client.beta.messages.stream(
                    model=self.model,
                    max_tokens=32000,
                    system=system,
                    messages=messages,
                    tools=tools,
                    output_config={"effort": effort},
                    betas=[FALLBACK_BETA],
                    fallbacks="default",
                ) as stream:
                    response = stream.get_final_message()
                if response.stop_reason == "refusal":
                    raise LLMError("Claude declined this request.")
                texts += [b.text for b in response.content if b.type == "text"]
                if response.stop_reason != "pause_turn":
                    break
                # the server-side search loop paused; send the turn back so it resumes
                messages = [messages[0], {"role": "assistant", "content": response.content}]
        except anthropic.AuthenticationError as e:
            raise LLMError("Anthropic rejected the API key (invalid or deleted). Paste a fresh key in Setup -> step 1.") from e
        except anthropic.RateLimitError as e:
            raise LLMError(f"Rate limited by the Anthropic API: {e.message}") from e
        except anthropic.APIStatusError as e:
            raise LLMError(f"Anthropic API error {e.status_code}: {e.message}") from e
        except anthropic.APIConnectionError as e:
            raise LLMError("Could not reach the Anthropic API.") from e
        return "\n".join(texts).strip()

"""
LLM Client for Agents
Handles Groq, Local Model, and Mercury with automatic fallback, circuit breaking, and caching.
"""

import os
import logging
import threading
import time
from typing import Optional, Dict, Any
import requests
from groq import Groq
from openai import OpenAI
from tenacity import retry, stop_after_attempt, wait_exponential, retry_if_exception_type

from cachetools import TTLCache

from .llm_config import (
    USE_LLM,
    ENABLE_FALLBACK,
    FALLBACK_CHAIN,
    GROQ_CONFIG,
    LOCAL_CONFIG,
    MERCURY_CONFIG,
    OPENAI_CONFIG,
)

logger = logging.getLogger(__name__)

# Cache configuration (100 items, 5 minutes TTL)
llm_cache = TTLCache(maxsize=100, ttl=300)

class AgentLLMClient:
    """
    Unified LLM client for agents with automatic fallback
    Supports: Groq, Local (Ollama), Mercury, OpenAI
    """

    def __init__(self):
        self.primary_provider = USE_LLM
        self.enable_fallback = ENABLE_FALLBACK
        self.fallback_chain = FALLBACK_CHAIN

        # Initialize clients
        self.groq_client = None
        self.openai_client = None
        self._init_groq()
        self._init_openai()

    def _init_groq(self):
        """Initialize Groq client"""
        try:
            api_key = os.getenv("GROQ_API_KEY")
            if api_key:
                self.groq_client = Groq(api_key=api_key)
                logger.info("Groq client initialized successfully")
            else:
                logger.warning("GROQ_API_KEY not found in environment")
        except Exception as e:
            logger.error(f"Failed to initialize Groq client: {e}")

    def _init_openai(self):
        """Initialize OpenAI client"""
        try:
            api_key = os.getenv("OPENAI_API_KEY")
            if api_key:
                self.openai_client = OpenAI(api_key=api_key)
                logger.info("OpenAI client initialized successfully")
            else:
                logger.warning("OPENAI_API_KEY not found in environment")
        except Exception as e:
            logger.error(f"Failed to initialize OpenAI client: {e}")

    def chat(
        self,
        messages: list,
        temperature: Optional[float] = None,
        max_tokens: Optional[int] = None,
        json_mode: bool = False,
    ) -> Optional[str]:
        """
        Send chat completion request with automatic fallback
        """
        # Check cache
        cache_key = str((messages, temperature, max_tokens, json_mode))
        if cache_key in llm_cache:
            logger.info("LLM Cache hit")
            return llm_cache[cache_key]

        start_time = time.time()
        response = None

        # Try primary provider first
        try:
            response = self._try_provider_with_retry(
                self.primary_provider, messages, temperature, max_tokens, json_mode
            )
        except Exception as e:
            logger.warning(f"Primary provider {self.primary_provider} failed: {e}")

        # Try fallback chain if enabled and primary failed
        if not response and self.enable_fallback:
            for provider in self.fallback_chain:
                if provider != self.primary_provider:
                    logger.info(f"Falling back to {provider}")
                    try:
                        response = self._try_provider_with_retry(
                            provider, messages, temperature, max_tokens, json_mode
                        )
                        if response:
                            break
                    except Exception as e:
                        logger.warning(f"Fallback provider {provider} failed: {e}")

        duration = time.time() - start_time
        
        if response:
            logger.info(f"LLM request completed in {duration:.2f}s using provider")
            llm_cache[cache_key] = response
            return response

        logger.error("All LLM providers failed")
        return None

    @retry(
        stop=stop_after_attempt(3),
        wait=wait_exponential(multiplier=1, min=4, max=10),
        retry=retry_if_exception_type((requests.RequestException, Exception)),
        reraise=True
    )
    def _try_provider_with_retry(
        self,
        provider: str,
        messages: list,
        temperature: Optional[float],
        max_tokens: Optional[int],
        json_mode: bool,
    ) -> Optional[str]:
        """Wrapper to apply retry logic to provider calls"""
        return self._try_provider(provider, messages, temperature, max_tokens, json_mode)

    def _try_provider(
        self,
        provider: str,
        messages: list,
        temperature: Optional[float],
        max_tokens: Optional[int],
        json_mode: bool,
    ) -> Optional[str]:
        """Try a specific provider"""
        if provider == "groq":
            return self._call_groq(messages, temperature, max_tokens, json_mode)
        elif provider == "local":
            return self._call_local(messages, temperature, max_tokens, json_mode)
        elif provider == "mercury":
            return self._call_mercury(messages, temperature, max_tokens, json_mode)
        elif provider == "openai":
            return self._call_openai(messages, temperature, max_tokens, json_mode)
        else:
            logger.error(f"Unknown provider: {provider}")
            raise ValueError(f"Unknown provider: {provider}")

    def _call_groq(
        self,
        messages: list,
        temperature: Optional[float],
        max_tokens: Optional[int],
        json_mode: bool,
    ) -> Optional[str]:
        """Call Groq API"""
        if not self.groq_client:
            raise Exception("Groq client not initialized")

        kwargs = {
            "model": GROQ_CONFIG["model"],
            "messages": messages,
            "temperature": temperature or GROQ_CONFIG["temperature"],
            "max_tokens": max_tokens or GROQ_CONFIG["max_tokens"],
        }

        if json_mode:
            kwargs["response_format"] = {"type": "json_object"}

        response = self.groq_client.chat.completions.create(**kwargs)
        return response.choices[0].message.content

    def _call_local(
        self,
        messages: list,
        temperature: Optional[float],
        max_tokens: Optional[int],
        json_mode: bool,
    ) -> Optional[str]:
        """Call local Ollama model"""
        url = f"{LOCAL_CONFIG['url']}/api/chat"

        payload = {
            "model": LOCAL_CONFIG["model"],
            "messages": messages,
            "stream": False,
            "options": {
                "temperature": temperature or LOCAL_CONFIG["temperature"],
                "num_predict": max_tokens or LOCAL_CONFIG["max_tokens"],
            },
        }

        if json_mode:
            payload["format"] = "json"

        response = requests.post(url, json=payload, timeout=60)
        response.raise_for_status()

        data = response.json()
        return data.get("message", {}).get("content")

    def _call_mercury(
        self,
        messages: list,
        temperature: Optional[float],
        max_tokens: Optional[int],
        json_mode: bool,
    ) -> Optional[str]:
        """Call Mercury API"""
        api_url = os.getenv("MERCURY_API_URL")
        api_key = os.getenv("MERCURY_API_KEY")

        if not api_url or not api_key:
            raise Exception("Mercury credentials not configured")

        # Adjust this based on your Mercury API format
        headers = {
            "Authorization": f"Bearer {api_key}",
            "Content-Type": "application/json",
        }

        payload = {
            "model": MERCURY_CONFIG["model"],
            "messages": messages,
            "temperature": temperature or MERCURY_CONFIG["temperature"],
            "max_tokens": max_tokens or MERCURY_CONFIG["max_tokens"],
        }

        if json_mode:
            payload["response_format"] = {"type": "json_object"}

        response = requests.post(
            f"{api_url}/chat/completions", headers=headers, json=payload, timeout=60
        )
        response.raise_for_status()

        data = response.json()
        return data.get("choices", [{}])[0].get("message", {}).get("content")

    def _call_openai(
        self,
        messages: list,
        temperature: Optional[float],
        max_tokens: Optional[int],
        json_mode: bool,
    ) -> Optional[str]:
        """Call OpenAI API"""
        if not self.openai_client:
            raise Exception("OpenAI client not initialized")

        kwargs = {
            "model": OPENAI_CONFIG["model"],
            "messages": messages,
            "temperature": temperature or OPENAI_CONFIG["temperature"],
            "max_tokens": max_tokens or OPENAI_CONFIG["max_tokens"],
        }

        if json_mode:
            kwargs["response_format"] = {"type": "json_object"}

        response = self.openai_client.chat.completions.create(**kwargs)
        return response.choices[0].message.content


# Singleton instance
_llm_client: Optional[AgentLLMClient] = None
_llm_client_lock = threading.Lock()


def get_llm_client() -> AgentLLMClient:
    """Get or create LLM client singleton (thread-safe double-checked locking)."""
    global _llm_client
    if _llm_client is None:
        with _llm_client_lock:
            if _llm_client is None:
                _llm_client = AgentLLMClient()
    return _llm_client
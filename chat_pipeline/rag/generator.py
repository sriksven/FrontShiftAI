"""Utilities that turn retrieved context into grounded LLM answers."""

from __future__ import annotations

import atexit
import logging
import os
import time
from dotenv import load_dotenv
from pathlib import Path
from typing import Any, Dict, Generator, Iterable, List, Literal, Optional, Tuple

from chat_pipeline.rag.config_manager import get_streaming_config, get_generation_config
from chat_pipeline.rag.prompt_templates import prompt_templates
from chat_pipeline.rag.reranker import two_stage_reranker
from chat_pipeline.rag.retriever import bm25_retrieval, vector_retrieval
from chat_pipeline.utils.runtime_env import (
    allow_heavy_fallbacks,
    remote_max_attempts,
    remote_retry_backoff,
    remote_retry_initial_delay,
    remote_timeout_seconds,
    remote_request_delay_seconds,
)

load_dotenv()

logger = logging.getLogger(__name__)

try:  # `llama_cpp` is optional – fall back to Mercury API when missing.
    from llama_cpp import Llama  # type: ignore
except ImportError:  # pragma: no cover - optional dependency
    Llama = None  # type: ignore

try:  # Hugging Face inference is an optional safety net.
    from huggingface_hub import InferenceClient  # type: ignore
except ImportError:  # pragma: no cover - optional dependency
    InferenceClient = None  # type: ignore

try:  # Requests is only required when hitting the remote Mercury API.
    import requests
    import httpx
except ImportError:  # pragma: no cover - optional dependency
    requests = None  # type: ignore
    httpx = None # type: ignore

try:
    from openai import OpenAI
except ImportError:
    OpenAI = None


try:  # Optional token-aware context truncation support.
    import tiktoken  # type: ignore
except ImportError:  # pragma: no cover - optional dependency
    tiktoken = None  # type: ignore


PROJECT_ROOT = Path(__file__).resolve().parents[2]
DEFAULT_MODEL_PATH = PROJECT_ROOT / "models" / "Llama-3.2-3B-Instruct-Q4_K_M.gguf"
DEFAULT_PROMPT_KEY = "general_prompt_1"
MAX_CONTEXT_CHARS = 6000
MAX_CONTEXT_TOKENS = int(os.getenv("MAX_CONTEXT_TOKENS", "2000"))
METADATA_MISMATCH_THRESHOLD = 5

DEFAULT_STREAMING_ARGS: Dict[str, Any] = {
    "max_tokens": 1024,
    "temperature": 0.6,
    "top_p": 0.9,
    "repeat_penalty": 1.1,
    "stop": ["---", "Thank you"],
}

INCEPTION_API_BASE = os.getenv("INCEPTION_API_BASE", "https://api.inceptionlabs.ai/v1")
INCEPTION_API_KEY = os.getenv("INCEPTION_API_KEY")
MERCURY_MODEL = os.getenv("MERCURY_MODEL", "mercury")
GROQ_API_KEY = os.getenv("GROQ_API_KEY")
GROQ_MODEL = os.getenv("GROQ_MODEL", "llama-3.1-8b-instant")

GENERATION_BACKEND = os.getenv("GENERATION_BACKEND")
HF_MODEL_NAME = os.getenv("HF_MODEL_NAME")
HF_API_TOKEN = os.getenv("HF_API_TOKEN")
HF_API_BASE = os.getenv("HF_API_BASE")
_LAST_BACKEND_USED: Optional[str] = None
REMOTE_TIMEOUT = remote_timeout_seconds()
REMOTE_MAX_ATTEMPTS = remote_max_attempts()
REMOTE_BACKOFF = remote_retry_backoff()
REMOTE_BACKOFF_START = remote_retry_initial_delay()
REMOTE_MIN_DELAY = remote_request_delay_seconds()

_LLM_INSTANCE: Optional[Any] = None
_LLM_CACHE_KEY: Optional[Tuple[str, Tuple[Tuple[str, Any], ...]]] = None
_HF_CLIENT: Optional[InferenceClient] = None  # type: ignore[assignment]


def _parse_retry_after(value: Any) -> Optional[float]:
    """Parse a Retry-After header value (seconds or HTTP-date) into float seconds."""
    if value is None:
        return None
    try:
        return max(0.0, float(value))
    except (TypeError, ValueError):
        pass
    try:
        from email.utils import parsedate_to_datetime
        from datetime import datetime, timezone
        dt = parsedate_to_datetime(str(value))
        if dt is None:
            return None
        if dt.tzinfo is None:
            dt = dt.replace(tzinfo=timezone.utc)
        delta = (dt - datetime.now(timezone.utc)).total_seconds()
        return max(0.0, delta)
    except Exception:
        return None

if tiktoken is not None:  # pragma: no branch - simple init
    try:
        _TOKEN_ENCODER = tiktoken.get_encoding(os.getenv("TIKTOKEN_ENCODING", "cl100k_base"))
    except Exception:  # pragma: no cover - optional dependency
        _TOKEN_ENCODER = None
else:
    _TOKEN_ENCODER = None


def _llm_init_kwargs(overrides: Optional[Dict[str, Any]] = None) -> Dict[str, Any]:
    """Compute constructor kwargs for ``llama_cpp.Llama``."""

    base_kwargs: Dict[str, Any] = {
        "n_ctx": int(os.getenv("LLAMA_CONTEXT", "4096")),
        "n_threads": int(os.getenv("LLAMA_THREADS", str(os.cpu_count() or 4))),
        "n_batch": int(os.getenv("LLAMA_BATCH", "512")),
        "temperature": float(os.getenv("LLAMA_TEMPERATURE", "0.7")),
        "top_p": float(os.getenv("LLAMA_TOP_P", "0.9")),
        "max_tokens": int(os.getenv("LLAMA_MAX_TOKENS", "1024")),
        "n_gpu": int(os.getenv("LLAMA_N_GPU_LAYERS", "-1")),
        "verbose": False,
    }
    if overrides:
        base_kwargs.update({k: v for k, v in overrides.items() if v is not None})
    return base_kwargs


def load_llm(
    model_path: Optional[os.PathLike[str] | str] = None,
    **overrides: Any,
) -> Any:
    """Load (and cache) the local LLaMA model instance."""

    if Llama is None:  # pragma: no cover - optional dependency
        raise RuntimeError(
            "llama_cpp is not installed. Install `llama-cpp-python` or "
            "provide INCEPTION_API_KEY for the Mercury fallback."
        )

    resolved_path = Path(
        model_path
        or os.getenv("LLAMA_MODEL_PATH")
        or DEFAULT_MODEL_PATH
    )
    if not resolved_path.exists():
        raise FileNotFoundError(
            f"LLaMA model not found at {resolved_path}. "
            "Set LLAMA_MODEL_PATH or drop the model into the /models directory."
        )

    init_kwargs = _llm_init_kwargs(overrides)
    cache_key = (str(resolved_path.resolve()), tuple(sorted(init_kwargs.items())))

    global _LLM_INSTANCE, _LLM_CACHE_KEY
    if _LLM_INSTANCE is not None and cache_key == _LLM_CACHE_KEY:
        return _LLM_INSTANCE

    logger.info("Loading LLaMA model from %s", resolved_path)
    _LLM_INSTANCE = Llama(model_path=str(resolved_path), **init_kwargs)
    _LLM_CACHE_KEY = cache_key
    return _LLM_INSTANCE


def _get_streaming_hyperparameters(overrides: Optional[Dict[str, Any]] = None) -> Dict[str, Any]:
    """Merge defaults, config file, and caller overrides into one dict."""

    cfg = get_streaming_config() or {}
    params = dict(DEFAULT_STREAMING_ARGS)
    params.update(cfg)
    if overrides:
        params.update({k: v for k, v in overrides.items() if v is not None})
    return params


def _get_hf_client():
    """Return a shared Hugging Face inference client (if configured)."""

    if not allow_heavy_fallbacks():
        raise RuntimeError("HF fallback disabled. Enable CHAT_PIPELINE_ALLOW_HEAVY_FALLBACKS to use it.")

    model_name = HF_MODEL_NAME or get_generation_config().get("hf_model_name")
    if not model_name:
        raise RuntimeError("HF_MODEL_NAME is not set.")
    if InferenceClient is None:
        raise RuntimeError("huggingface_hub is not installed.")

    global _HF_CLIENT
    if _HF_CLIENT is None:
        logger.info("Connecting to Hugging Face Inference API model %s", model_name)
        client_kwargs: Dict[str, Any] = {"model": model_name}
        if HF_API_TOKEN:
            client_kwargs["token"] = HF_API_TOKEN
        if HF_API_BASE:
            client_kwargs["base_url"] = HF_API_BASE
        _HF_CLIENT = InferenceClient(**client_kwargs)
    return _HF_CLIENT


def _stream_from_local_llm(llm: Any, prompt: str, params: Dict[str, Any]) -> Generator[str, None, None]:
    """Yield tokens from the local LLaMA model."""

    completion_kwargs = dict(params)
    completion_kwargs["stream"] = True
    # Remove keys that llama_cpp doesn't understand
    completion_kwargs.pop("system_message", None)
    completion_kwargs.pop("user_message", None)
    
    response_stream = llm.create_completion(prompt=prompt, **completion_kwargs)

    for chunk in response_stream:
        token = ""
        if isinstance(chunk, dict):
            choices = chunk.get("choices") or []
            if choices:
                token = choices[0].get("text", "") or ""
        if token and token.strip():
            yield token


def _stream_from_hf(prompt: str, params: Dict[str, Any]) -> Generator[str, None, None]:
    """Yield tokens from the Hugging Face Inference API."""

    client = _get_hf_client()
    stream = client.text_generation(
        prompt,
        max_new_tokens=params.get("max_tokens"),
        temperature=params.get("temperature"),
        top_p=params.get("top_p"),
        stream=True,
        return_full_text=False,
    )

    for chunk in stream:
        if isinstance(chunk, str):
            token = chunk
        else:  # huggingface_hub returns TextGenerationStreamResponse objects
            token = getattr(chunk, "token", None)
            if token is not None:
                token = getattr(token, "text", None)
        if token and token.strip():
            yield token


def _call_mercury_api(prompt: str, params: Dict[str, Any]) -> str:
    """Call the Inception Labs Mercury API as a fallback."""

    if not INCEPTION_API_KEY:
        raise RuntimeError(
            "INCEPTION_API_KEY is not set and local LLaMA is unavailable. "
            "Set the environment variable to enable the Mercury fallback."
        )
    if requests is None:  # pragma: no cover - optional dependency
        raise RuntimeError(
            "The `requests` package is required for the Mercury fallback. "
            "Install it via `pip install requests`."
        )

    headers = {
        "Authorization": f"Bearer {INCEPTION_API_KEY}",
        "Content-Type": "application/json",
    }
    system_msg = params.get("system_message")
    user_msg = params.get("user_message")

    messages = []
    if system_msg and user_msg:
        messages.append({"role": "system", "content": system_msg})
        messages.append({"role": "user", "content": user_msg})
    else:
        messages.append({"role": "user", "content": prompt})

    payload = {
        "model": MERCURY_MODEL,
        "messages": messages,
        "max_tokens": params.get("max_tokens"),
        "temperature": params.get("temperature"),
        "top_p": params.get("top_p"),
    }
    attempts = REMOTE_MAX_ATTEMPTS
    delay = REMOTE_BACKOFF_START
    last_exc: Exception | None = None
    for attempt in range(1, attempts + 1):
        try:
            if REMOTE_MIN_DELAY > 0 and attempt > 1:
                time.sleep(REMOTE_MIN_DELAY)

            # Using httpx for better stability in Cloud Run (avoids potential requests/urllib3 SIGABRT)
            with httpx.Client(timeout=REMOTE_TIMEOUT) as client:
                response = client.post(
                    f"{INCEPTION_API_BASE}/chat/completions",
                    json=payload,
                    headers=headers,
                )
            response.raise_for_status()
            data = response.json()
            if "choices" in data and data["choices"]:
                message = data["choices"][0].get("message", {})
                return (message.get("content") or "").strip()
            if "content" in data:
                return (data["content"] or "").strip()
            raise RuntimeError(f"Unexpected response format from Mercury API: {data}")
        except Exception as exc:
            last_exc = exc

            # Honor 429 Retry-After before falling through to normal exp backoff.
            retry_after: Optional[float] = None
            status_code = None
            resp_obj = getattr(exc, "response", None)
            if resp_obj is not None:
                status_code = getattr(resp_obj, "status_code", None)
                if status_code == 429:
                    try:
                        retry_after = _parse_retry_after(resp_obj.headers.get("Retry-After"))
                    except Exception:
                        retry_after = None

            if status_code == 429:
                sleep_for = retry_after if retry_after is not None else delay
                logger.warning(
                    "Mercury API 429 on attempt %s/%s. Sleeping %.1fs before retry.",
                    attempt, attempts, sleep_for,
                )
                if attempt == attempts:
                    break
                time.sleep(sleep_for)
                delay *= REMOTE_BACKOFF
                continue

            logger.warning("Mercury API attempt %s/%s failed: %s", attempt, attempts, exc)
            if attempt == attempts:
                break
            time.sleep(delay)
            delay *= REMOTE_BACKOFF

    raise RuntimeError("Mercury API failed after multiple attempts.") from last_exc


def _call_groq_api(prompt: str, params: Dict[str, Any]) -> str:
    """Call the Groq API (OpenAI-compatible) as a backend."""

    if not GROQ_API_KEY:
        raise RuntimeError("GROQ_API_KEY is not set. Set the environment variable to use the Groq backend.")
    if requests is None:
        raise RuntimeError("The `requests` package is required for the Groq backend.")

    headers = {
        "Authorization": f"Bearer {GROQ_API_KEY}",
        "Content-Type": "application/json",
    }
    system_msg = params.get("system_message")
    user_msg = params.get("user_message")

    messages = []
    if system_msg and user_msg:
        messages.append({"role": "system", "content": system_msg})
        messages.append({"role": "user", "content": user_msg})
    else:
        messages.append({"role": "user", "content": prompt})

    payload = {
        "model": GROQ_MODEL,
        "messages": messages,
        "max_tokens": params.get("max_tokens"),
        "temperature": params.get("temperature"),
        "top_p": params.get("top_p"),
    }
    
    attempts = REMOTE_MAX_ATTEMPTS
    delay = REMOTE_BACKOFF_START
    last_exc: Exception | None = None

    for attempt in range(1, attempts + 1):
        try:
            if REMOTE_MIN_DELAY > 0 and attempt > 1:
                time.sleep(REMOTE_MIN_DELAY)
            
            # Groq uses standard OpenAI-compatible endpoint structure
            response = requests.post(
                "https://api.groq.com/openai/v1/chat/completions",
                json=payload,
                headers=headers,
                timeout=REMOTE_TIMEOUT,
            )
            response.raise_for_status()
            data = response.json()
            if "choices" in data and data["choices"]:
                message = data["choices"][0].get("message", {})
                return (message.get("content") or "").strip()
            raise RuntimeError(f"Unexpected response format from Groq API: {data}")
            
        except Exception as exc:
            last_exc = exc
            
            # Basic 429 handling for Groq as well
            is_rate_limit = False
            if isinstance(exc, requests.exceptions.HTTPError):
                if getattr(exc.response, "status_code", None) == 429:
                    is_rate_limit = True
            
            if is_rate_limit:
                 logger.warning("Groq API rate limit (429) hit on attempt %s/%s. Pausing 10s...", attempt, attempts)
                 time.sleep(10.0)
            else:
                logger.warning("Groq API attempt %s/%s failed: %s", attempt, attempts, exc)
                time.sleep(delay)
                delay *= REMOTE_BACKOFF
                
            if attempt == attempts:
                break

    raise RuntimeError("Groq API failed after multiple attempts.") from last_exc


def _call_openai_api(prompt: str, params: Dict[str, Any]) -> str:
    """Call the OpenAI API as a backend."""

    api_key = os.getenv("OPENAI_API_KEY")
    if not api_key:
        raise RuntimeError("OPENAI_API_KEY is not set.")
    if OpenAI is None:
        raise RuntimeError("The `openai` package is required for the OpenAI backend.")

    client = OpenAI(api_key=api_key)
    
    system_msg = params.get("system_message")
    user_msg = params.get("user_message")

    messages = []
    if system_msg and user_msg:
        messages.append({"role": "system", "content": system_msg})
        messages.append({"role": "user", "content": user_msg})
    else:
        messages.append({"role": "user", "content": prompt})

    model = os.getenv("OPENAI_MODEL", "gpt-4o-mini")

    attempts = REMOTE_MAX_ATTEMPTS
    delay = REMOTE_BACKOFF_START
    last_exc: Exception | None = None
    for attempt in range(1, attempts + 1):
        try:
            response = client.chat.completions.create(
                model=model,
                messages=messages,
                max_tokens=params.get("max_tokens"),
                temperature=params.get("temperature"),
                top_p=params.get("top_p"),
                timeout=REMOTE_TIMEOUT,
            )
            return (response.choices[0].message.content or "").strip()
        except Exception as exc:
            last_exc = exc

            status_code = getattr(exc, "status_code", None)
            if status_code is None:
                resp_obj = getattr(exc, "response", None)
                if resp_obj is not None:
                    status_code = getattr(resp_obj, "status_code", None)

            if status_code == 429:
                retry_after = None
                resp_obj = getattr(exc, "response", None)
                if resp_obj is not None:
                    try:
                        retry_after = _parse_retry_after(resp_obj.headers.get("Retry-After"))
                    except Exception:
                        retry_after = None
                sleep_for = retry_after if retry_after is not None else delay
                logger.warning(
                    "OpenAI API 429 on attempt %s/%s. Sleeping %.1fs before retry.",
                    attempt, attempts, sleep_for,
                )
                if attempt == attempts:
                    break
                time.sleep(sleep_for)
                delay *= REMOTE_BACKOFF
                continue

            logger.warning("OpenAI API attempt %s/%s failed: %s", attempt, attempts, exc)
            if attempt == attempts:
                break
            time.sleep(delay)
            delay *= REMOTE_BACKOFF

    logger.error("OpenAI API call failed after %s attempts: %s", attempts, last_exc)
    raise RuntimeError("OpenAI API failed after multiple attempts.") from last_exc


def _record_backend(name: Optional[str]) -> None:
    global _LAST_BACKEND_USED
    _LAST_BACKEND_USED = name


def get_last_backend_used() -> Optional[str]:
    """Return the backend picked during the most recent generation call."""

    return _LAST_BACKEND_USED


def stream_response(
    prompt: str,
    llm: Optional[Any] = None,
    **stream_overrides: Any,
) -> Generator[str, None, None]:
    """Stream the model output token-by-token as it is generated, with fallback support."""

    _record_backend(None)
    params = _get_streaming_hyperparameters(stream_overrides)
    gen_cfg = get_generation_config()
    
    # Priority order: Configured backend -> Mercury -> Groq -> OpenAI -> Local
    # If the user specifically set GENERATION_BACKEND, we try ONLY that first, but we can implement fallbacks even then if desired.
    # However, for this requirement: "if rag is triggered - LLM used is mercury, with groq and lopenai as a fallback"
    # We will enforce this order if 'auto' or unset.
    
    configured_backend = (GENERATION_BACKEND or gen_cfg.get("backend") or "auto").lower()

    # Define the chain based on user requirement
    # Mercury -> Groq -> OpenAI -> Local (via 'auto'/'local' check)
    
    chain = []
    
    if configured_backend != "auto" and configured_backend in ["mercury", "groq", "openai", "local", "hf"]:
        # If user explicitly set one, put it first. The others follow as fallback if we want robust fallback.
        # But if they set it explicitly, maybe they ONLY want that? 
        # The user request implies a general policy: "if rag is triggered... mercury, with groq and lopenai as a fallback"
        # So we should probably construct the chain starting with Mercury.
        chain.append(configured_backend)
    
    # Standard fallback chain per request
    # avoid duplicates
    for provider in ["mercury", "groq", "openai", "local"]:
        if provider not in chain:
            chain.append(provider)
            
    # Now try them in order
    last_error = None
    
    for backend in chain:
        try:
            logger.info(f"Attempting generation with backend: {backend}")
            
            if backend == "groq":
                _record_backend("groq")
                yield _call_groq_api(prompt, params)
                return

            if backend == "openai":
                _record_backend("openai")
                yield _call_openai_api(prompt, params)
                return

            if backend == "mercury":
                _record_backend("mercury")
                yield _call_mercury_api(prompt, params)
                return
                
            if backend == "local":
                 # Prepare local LLM
                 local_llm = llm
                 if local_llm is None:
                     try:
                         local_llm = load_llm()
                     except Exception as exc:
                         logger.warning(f"Local LLM load failed: {exc}")
                         continue
                         
                 if local_llm is not None:
                    _record_backend("local")
                    yield from _stream_from_local_llm(local_llm, prompt, params)
                    return
        
        except KeyboardInterrupt:
            raise
        except Exception as e:
            logger.warning(f"Backend {backend} failed: {e}")
            last_error = e
            continue
            
    # If we get here, all failed
    logger.error("All RAG generation backends failed.")
    if last_error:
        raise last_error
    raise RuntimeError("All RAG generation backends failed.")


def _select_prompt_template(template_key: Optional[str]) -> str:
    """Return the requested system prompt template (with fallback)."""

    key = template_key or DEFAULT_PROMPT_KEY
    template = prompt_templates.get(key) or prompt_templates.get(DEFAULT_PROMPT_KEY, "")
    template = template.strip()
    if not template:
        logger.warning(
            "Prompt template '%s' is empty. Falling back to a minimal instruction.",
            key,
        )
        return "You are a helpful assistant. Use the provided context."
    return template


def _build_prompt(template: str, context: str, query: str) -> str:
    """Assemble the final prompt fed to the model."""

    return f"""{template}

### CONTEXT:
{context}

### QUESTION:
{query}

### ANSWER:""".strip()


def _prepare_context(documents: Iterable[str]) -> str:
    """Join retrieved documents into a bounded text block."""

    context = "\n\n".join(documents)
    return _truncate_context(context)


def _truncate_context(context: str) -> str:
    if MAX_CONTEXT_TOKENS and MAX_CONTEXT_TOKENS > 0 and _TOKEN_ENCODER is not None:
        tokens = _TOKEN_ENCODER.encode(context)
        if len(tokens) <= MAX_CONTEXT_TOKENS:
            return context
        truncated = _TOKEN_ENCODER.decode(tokens[:MAX_CONTEXT_TOKENS])
        return f"{truncated}..."
    if len(context) > MAX_CONTEXT_CHARS:
        return f"{context[:MAX_CONTEXT_CHARS]}..."
    return context


def _run_retrieval(
    query: str,
    retriever: Literal["vector", "bm25"],
    top_k: int,
    company_name: Optional[str],
    reranker: Optional[str],
    rerank_k: Optional[int],
    max_documents: Optional[int],
) -> Tuple[List[str], List[Dict[str, Any]]]:
    """Retrieve (and optionally rerank) supporting documents."""

    retriever_key = (retriever or "vector").lower()
    use_reranker = bool(reranker)

    if use_reranker:
        reranked = two_stage_reranker(
            query=query,
            retrieval=retriever_key,  # type: ignore[arg-type]
            top_k=top_k,
            rerank_k=rerank_k,
            company_name=company_name,
            max_documents=max_documents,
        )
        docs = [item["document"] for item in reranked]
        metadata: List[Dict[str, Any]] = []
        for item in reranked:
            meta = dict(item.get("metadata", {}) or {})
            score = item.get("score")
            if score is not None:
                try:
                    meta["reranker_score"] = float(score)
                except (TypeError, ValueError):
                    meta["reranker_score"] = score
            metadata.append(meta)
        return docs, metadata

    if retriever_key not in {"vector", "bm25"}:
        raise ValueError("retriever must be 'vector' or 'bm25'")

    if retriever_key == "vector":
        docs, metadata = vector_retrieval(
            query=query,
            top_k=top_k,
            company_name=company_name,
        )
    else:
        docs, metadata = bm25_retrieval(
            query=query,
            top_k=top_k,
            company_name=company_name,
            max_documents=max_documents,
        )

    docs = docs or []
    metadata = _align_metadata(docs, metadata)
    return docs, metadata


def _align_metadata(docs: List[str], metadata: Optional[List[Dict[str, Any]]]) -> List[Dict[str, Any]]:
    metadata = list(metadata or [])
    if len(metadata) == len(docs):
        return metadata

    doc_count = len(docs)
    mismatch = abs(len(metadata) - doc_count)
    if mismatch > METADATA_MISMATCH_THRESHOLD:
        raise ValueError(
            f"Metadata/doc mismatch detected (docs={doc_count}, metadata={len(metadata)})."
        )

    logger.warning(
        "Aligning metadata (%s) to documents (%s). Consider investigating upstream chunking.",
        len(metadata),
        doc_count,
    )

    if len(metadata) < doc_count:
        metadata.extend({} for _ in range(doc_count - len(metadata)))
    else:
        metadata = metadata[:doc_count]
    return [dict(item or {}) for item in metadata]


def generation(
    query: str,
    company_name: Optional[str] = None,
    retriever: Literal["vector", "bm25"] = "vector",
    reranker: Optional[str] = None,
    top_k: int = 5,
    rerank_k: Optional[int] = None,
    template_key: Optional[str] = None,
    stream: bool = False,
    streaming_overrides: Optional[Dict[str, Any]] = None,
    llm: Optional[Any] = None,
    max_documents: Optional[int] = None,
    documents: Optional[List[str]] = None,
    metadatas: Optional[List[Dict[str, Any]]] = None,
) -> Tuple[Any, List[Dict[str, Any]]]:
    """High-level RAG helper that returns an answer and its supporting metadata.

    Parameters
    ----------
    query:
        Natural language question from the user.
    company_name:
        Optional company filter propagated to the retrievers.
    retriever:
        Retrieval backend to use – ``"vector"`` or ``"bm25"``.
    reranker:
        If provided, runs :func:`two_stage_reranker` instead of returning the
        raw retriever output.
    top_k:
        Number of documents to fetch from the retriever layer.
    rerank_k:
        Optional number of documents to keep after reranking.
    template_key:
        Key from :mod:`prompt_templates` that defines the system prompt.
    stream:
        When ``True``, returns a generator that yields tokens; otherwise a
        fully materialised string.
    streaming_overrides:
        Optional overrides for streaming hyperparameters.
    llm:
        Pass an already-loaded ``llama_cpp.Llama`` instance (skips reloads).
    max_documents:
        Upper bound for lexical retrievers that materialise LangChain docs.
    documents:
        Optional pre-retrieved context chunks. When supplied, the retriever
        stage is skipped.
    metadatas:
        Optional metadata aligned with ``documents``.

    Returns
    -------
    Tuple[Any, List[Dict[str, Any]]]
        The first element is either the final answer string (when
        ``stream=False``) or a generator yielding tokens. The second element
        contains the metadata for each supporting chunk.
    """

    docs = documents
    metadata = metadatas

    if docs is None:
        docs, metadata = _run_retrieval(
            query=query,
            retriever=retriever,
            top_k=top_k,
            company_name=company_name,
            reranker=reranker,
            rerank_k=rerank_k,
            max_documents=max_documents,
        )

    docs = docs or []
    metadata = _align_metadata(docs, metadata)

    if not docs:
        return "No relevant context found in the company handbook.", []

    prompt_template = _select_prompt_template(template_key)
    context = _prepare_context(docs)
    prompt = _build_prompt(prompt_template, context, query)

    # Prepare structured messages for chat backends
    overrides = streaming_overrides or {}
    overrides["system_message"] = prompt_template
    overrides["user_message"] = f"### CONTEXT:\n{context}\n\n### QUESTION:\n{query}"

    response_iter = stream_response(
        prompt,
        llm=llm,
        **overrides,
    )

    if stream:
        return response_iter, metadata

    answer = "".join(response_iter).strip()
    return answer, metadata


__all__ = [
    "load_llm",
    "stream_response",
    "generation",
    "get_last_backend_used",
    "DEFAULT_MODEL_PATH",
    "HF_MODEL_NAME",
    "HF_MODEL_NAME",
    "INCEPTION_API_KEY",
    "GROQ_API_KEY",
]
def _close_llm() -> None:
    """Ensure the cached LLaMA instance is cleanly closed at shutdown."""

    global _LLM_INSTANCE
    if _LLM_INSTANCE is None:
        return

    close_fn = getattr(_LLM_INSTANCE, "close", None)
    if callable(close_fn):
        try:
            close_fn()
        except Exception as exc:  # pragma: no cover - defensive cleanup
            logger.debug("Failed to close LLaMA instance: %s", exc)
    _LLM_INSTANCE = None


atexit.register(_close_llm)



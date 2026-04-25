"""High-level orchestrator that wires retrieval, reranking, and generation."""

from __future__ import annotations

import argparse
import copy
import inspect
import json
import logging
import os
import threading
import time
from collections import OrderedDict
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, Dict, Iterable, List, Optional, Tuple

from chat_pipeline.rag.config_manager import load_rag_config
from chat_pipeline.rag.generator import (
    DEFAULT_MODEL_PATH,
    HF_MODEL_NAME,
    INCEPTION_API_KEY,
    generation,
    get_last_backend_used,
)
from chat_pipeline.rag.reranker import two_stage_reranker
from chat_pipeline.rag.retriever import bm25_retrieval, vector_retrieval


logger = logging.getLogger(__name__)

RETRIEVER_REGISTRY = {
    "vector": vector_retrieval,
    "bm25": bm25_retrieval,
}

RERANKER_REGISTRY = {
    "two_stage": two_stage_reranker,
    "cross_encoder": two_stage_reranker,  # alias for readability
}

DEFAULT_CACHE_SIZE = 32


def _deep_merge(base: Dict[str, Any], override: Optional[Dict[str, Any]]) -> Dict[str, Any]:
    """Recursively merge dictionaries without mutating the originals."""

    if not override:
        return copy.deepcopy(base)

    merged = copy.deepcopy(base)
    for key, value in override.items():
        if (
            key in merged
            and isinstance(merged[key], dict)
            and isinstance(value, dict)
        ):
            merged[key] = _deep_merge(merged[key], value)
        else:
            merged[key] = value
    return merged


def load_pipeline_config(overrides: Optional[Dict[str, Any]] = None) -> Dict[str, Any]:
    """Return the pipeline portion of ``rag.yaml`` (plus optional overrides)."""

    config = load_rag_config().get("pipeline", {})
    return _deep_merge(config, overrides or {})


def _call_component(component: Any, **kwargs: Any):
    """Invoke a component with only the parameters it supports."""

    signature = inspect.signature(component)
    supported_kwargs = {
        name: value
        for name, value in kwargs.items()
        if name in signature.parameters and value is not None
    }
    return component(**supported_kwargs)


@dataclass
class RetrieverSettings:
    name: str = "vector"
    top_k: int = 5
    max_documents: Optional[int] = None

    @classmethod
    def from_dict(cls, data: Dict[str, Any]) -> "RetrieverSettings":
        return cls(
            name=(data.get("name") or data.get("type") or "vector").lower(),
            top_k=int(data.get("top_k", 5)),
            max_documents=data.get("max_documents"),
        )

    def to_dict(self) -> Dict[str, Any]:
        return {
            "name": self.name,
            "top_k": self.top_k,
            "max_documents": self.max_documents,
        }


@dataclass
class RerankerSettings:
    enabled: bool = False
    strategy: Optional[str] = "two_stage"
    rerank_k: Optional[int] = None
    batch_size: Optional[int] = None

    @classmethod
    def from_dict(cls, data: Dict[str, Any]) -> "RerankerSettings":
        return cls(
            enabled=bool(data.get("enabled", False)),
            strategy=(data.get("strategy") or data.get("name")),
            rerank_k=data.get("rerank_k"),
            batch_size=data.get("batch_size"),
        )

    def to_dict(self) -> Dict[str, Any]:
        return {
            "enabled": self.enabled,
            "strategy": self.strategy,
            "rerank_k": self.rerank_k,
            "batch_size": self.batch_size,
        }


@dataclass
class GenerationSettings:
    template_key: Optional[str] = None
    stream: bool = False
    streaming_overrides: Dict[str, Any] = field(default_factory=dict)

    @classmethod
    def from_dict(cls, data: Dict[str, Any]) -> "GenerationSettings":
        return cls(
            template_key=data.get("template_key"),
            stream=bool(data.get("stream", False)),
            streaming_overrides=dict(data.get("streaming_overrides") or {}),
        )

    def to_dict(self) -> Dict[str, Any]:
        return {
            "template_key": self.template_key,
            "stream": self.stream,
            "streaming_overrides": dict(self.streaming_overrides),
        }


@dataclass
class PipelineConfig:
    retriever: RetrieverSettings = field(default_factory=RetrieverSettings)
    reranker: RerankerSettings = field(default_factory=RerankerSettings)
    generation: GenerationSettings = field(default_factory=GenerationSettings)

    @classmethod
    def from_dict(cls, data: Dict[str, Any]) -> "PipelineConfig":
        return cls(
            retriever=RetrieverSettings.from_dict(data.get("retriever", {})),
            reranker=RerankerSettings.from_dict(data.get("reranker", {})),
            generation=GenerationSettings.from_dict(data.get("generation", {})),
        )

    def to_dict(self) -> Dict[str, Any]:
        return {
            "retriever": self.retriever.to_dict(),
            "reranker": self.reranker.to_dict(),
            "generation": self.generation.to_dict(),
        }


@dataclass
class PipelineResult:
    """Container returned by :meth:`RAGPipeline.run`."""

    answer: Any
    metadata: List[Dict[str, Any]]
    streamed: bool
    config: PipelineConfig
    timings: Dict[str, float] = field(default_factory=dict)
    generation_backend: Optional[str] = None

    def is_stream(self) -> bool:
        return self.streamed


class RAGPipeline:
    """Config-driven RAG orchestrator with swappable modules."""

    def __init__(
        self,
        config_overrides: Optional[Dict[str, Any]] = None,
        *,
        cache_size: int = DEFAULT_CACHE_SIZE,
    ):
        self.retriever_registry = dict(RETRIEVER_REGISTRY)
        self.reranker_registry = dict(RERANKER_REGISTRY)
        self._base_config = load_pipeline_config(config_overrides)
        self.config = PipelineConfig.from_dict(self._base_config)
        self.cache_size = max(int(cache_size), 0)
        self._cache: OrderedDict[str, Tuple[str, List[Dict[str, Any]]]] = OrderedDict()
        self._cache_lock = threading.Lock()

    # ------------------------------------------------------------------ #
    # Component registration helpers
    # ------------------------------------------------------------------ #
    def register_retriever(self, name: str, func: Any) -> None:
        self.retriever_registry[name.lower()] = func

    def register_reranker(self, name: str, func: Any) -> None:
        self.reranker_registry[name.lower()] = func

    # ------------------------------------------------------------------ #
    # Public helpers
    # ------------------------------------------------------------------ #
    def describe(self) -> Dict[str, Any]:
        """Return the current pipeline configuration as a plain dict."""

        payload = self.config.to_dict()
        payload["model"] = self._model_descriptor()
        payload["cache_size"] = self.cache_size
        return payload

    def run(
        self,
        query: str,
        company_name: Optional[str] = None,
        *,
        config_overrides: Optional[Dict[str, Any]] = None,
        retriever: Optional[str] = None,
        top_k: Optional[int] = None,
        reranker_enabled: Optional[bool] = None,
        reranker_strategy: Optional[str] = None,
        rerank_k: Optional[int] = None,
        template_key: Optional[str] = None,
        stream: Optional[bool] = None,
        streaming_overrides: Optional[Dict[str, Any]] = None,
        max_documents: Optional[int] = None,
    ) -> PipelineResult:
        """Execute the configured RAG pipeline."""

        runtime_overrides: Dict[str, Any] = {}
        if retriever is not None or top_k is not None or max_documents is not None:
            runtime_overrides.setdefault("retriever", {})
            if retriever is not None:
                runtime_overrides["retriever"]["name"] = retriever
            if top_k is not None:
                runtime_overrides["retriever"]["top_k"] = top_k
            if max_documents is not None:
                runtime_overrides["retriever"]["max_documents"] = max_documents

        if (
            reranker_enabled is not None
            or reranker_strategy is not None
            or rerank_k is not None
        ):
            runtime_overrides.setdefault("reranker", {})
            if reranker_enabled is not None:
                runtime_overrides["reranker"]["enabled"] = reranker_enabled
            if reranker_strategy is not None:
                runtime_overrides["reranker"]["strategy"] = reranker_strategy
            if rerank_k is not None:
                runtime_overrides["reranker"]["rerank_k"] = rerank_k

        if (
            template_key is not None
            or stream is not None
            or streaming_overrides is not None
        ):
            runtime_overrides.setdefault("generation", {})
            if template_key is not None:
                runtime_overrides["generation"]["template_key"] = template_key
            if stream is not None:
                runtime_overrides["generation"]["stream"] = stream
            if streaming_overrides:
                current = runtime_overrides["generation"].setdefault(
                    "streaming_overrides", {}
                )
                current.update(streaming_overrides)

        combined_overrides = _deep_merge(runtime_overrides, config_overrides or {})
        settings = self._resolve_settings(combined_overrides)

        effective_stream = stream if stream is not None else settings.generation.stream
        effective_stream_overrides = {
            **settings.generation.streaming_overrides,
            **(streaming_overrides or {}),
        }
        timings: Dict[str, float] = {}

        cache_key: Optional[str] = None
        cache_hit = False
        use_cache = not effective_stream and self.cache_size > 0
        if use_cache:
            cache_key = self._make_cache_key(query, company_name, settings)
            with self._cache_lock:
                cached = self._cache.get(cache_key)
                if cached is not None:
                    self._cache.move_to_end(cache_key)
                    cached_answer, cached_metadata = cached
                    cached_metadata_copy = copy.deepcopy(cached_metadata)
                    cache_hit = True
            if cache_hit:
                timings["cache_hit"] = 1.0
                return PipelineResult(
                    answer=cached_answer,
                    metadata=cached_metadata_copy,
                    streamed=False,
                    config=settings,
                    timings=timings,
                )

        start = time.perf_counter()
        self._validate_components(settings)
        docs, metadata = self._execute_retrieval(query, company_name, settings)
        timings["retrieval"] = time.perf_counter() - start

        if not docs:
            return PipelineResult(
                answer="No relevant context found in the company handbook.",
                metadata=[],
                streamed=False,
                config=settings,
                timings=timings,
            )

        gen_start = time.perf_counter()
        answer, metadata = generation(
            query=query,
            company_name=company_name,
            retriever=settings.retriever.name,
            reranker=(
                settings.reranker.strategy if settings.reranker.enabled else None
            ),
            top_k=settings.retriever.top_k,
            rerank_k=settings.reranker.rerank_k,
            template_key=settings.generation.template_key,
            stream=effective_stream,
            streaming_overrides=effective_stream_overrides,
            max_documents=settings.retriever.max_documents,
            documents=docs,
            metadatas=metadata,
        )
        backend_used = get_last_backend_used()
        timings["generation"] = time.perf_counter() - gen_start
        timings["cache_hit"] = 1.0 if cache_hit else 0.0

        if use_cache and cache_key and not effective_stream and isinstance(answer, str):
            self._update_cache(cache_key, answer, metadata)

        return PipelineResult(
            answer=answer,
            metadata=metadata,
            streamed=effective_stream,
            config=settings,
            timings=timings,
            generation_backend=backend_used,
        )

    # ------------------------------------------------------------------ #
    # Internal helpers
    # ------------------------------------------------------------------ #
    def _resolve_settings(self, overrides: Optional[Dict[str, Any]]) -> PipelineConfig:
        config_dict = _deep_merge(self._base_config, overrides or {})
        return PipelineConfig.from_dict(config_dict)

    def _make_cache_key(
        self,
        query: str,
        company_name: Optional[str],
        settings: PipelineConfig,
    ) -> str:
        payload = {
            "query": query,
            "company_name": company_name,
            "config": settings.to_dict(),
        }
        return json.dumps(payload, sort_keys=True)

    def _update_cache(
        self,
        cache_key: str,
        answer: str,
        metadata: List[Dict[str, Any]],
    ) -> None:
        if self.cache_size <= 0:
            return
        metadata_copy = copy.deepcopy(metadata)
        with self._cache_lock:
            self._cache[cache_key] = (answer, metadata_copy)
            self._cache.move_to_end(cache_key)
            while len(self._cache) > self.cache_size:
                self._cache.popitem(last=False)

    def _validate_components(self, settings: PipelineConfig) -> None:
        retriever_name = settings.retriever.name
        if retriever_name not in self.retriever_registry:
            raise ValueError(
                f"Unknown retriever '{retriever_name}'. "
                f"Known options: {', '.join(self.retriever_registry)}"
            )

        if settings.reranker.enabled:
            strategy = (settings.reranker.strategy or "").lower()
            if strategy not in self.reranker_registry:
                raise ValueError(
                    f"Unknown reranker '{settings.reranker.strategy}'. "
                    f"Known options: {', '.join(self.reranker_registry)}"
                )

    def _execute_retrieval(
        self,
        query: str,
        company_name: Optional[str],
        settings: PipelineConfig,
    ) -> Tuple[List[str], List[Dict[str, Any]]]:
        retriever_name = settings.retriever.name.lower()

        if settings.reranker.enabled and settings.reranker.strategy:
            reranker_name = settings.reranker.strategy.lower()
            reranker_fn = self.reranker_registry[reranker_name]
            logger.debug(
                "Running reranker '%s' over retriever '%s' (top_k=%s, rerank_k=%s)",
                reranker_name,
                retriever_name,
                settings.retriever.top_k,
                settings.reranker.rerank_k,
            )
            reranked = reranker_fn(
                query=query,
                retrieval=retriever_name,
                top_k=settings.retriever.top_k,
                rerank_k=settings.reranker.rerank_k,
                company_name=company_name,
                max_documents=settings.retriever.max_documents,
                batch_size=settings.reranker.batch_size,
            )
            docs = [item.get("document", "") for item in reranked]
            metadata = []
            for item in reranked:
                meta = dict(item.get("metadata", {}) or {})
                score = item.get("score")
                if score is not None:
                    try:
                        meta["reranker_score"] = float(score)
                    except (TypeError, ValueError):
                        meta["reranker_score"] = score
                metadata.append(meta)
            return self._filter_by_company(company_name, docs, metadata)

        retriever_fn = self.retriever_registry[retriever_name]
        logger.debug(
            "Running retriever '%s' (top_k=%s)",
            retriever_name,
            settings.retriever.top_k,
        )
        docs, metadata = _call_component(
            retriever_fn,
            query=query,
            top_k=settings.retriever.top_k,
            company_name=company_name,
            max_documents=settings.retriever.max_documents,
        )
        docs = docs or []
        metadata = metadata or [{} for _ in docs]
        return self._filter_by_company(company_name, docs, metadata)

    def _filter_by_company(
        self,
        company_name: Optional[str],
        docs: List[str],
        metadata: List[Dict[str, Any]],
    ) -> Tuple[List[str], List[Dict[str, Any]]]:
        if not company_name:
            return docs, metadata

        normalized_target = company_name.strip().lower()
        filtered_docs: List[str] = []
        filtered_meta: List[Dict[str, Any]] = []
        for doc, meta in zip(docs, metadata):
            company_value = (meta.get("company") or "").lower()
            if normalized_target in company_value:
                filtered_docs.append(doc)
                filtered_meta.append(meta)

        if filtered_docs:
            return filtered_docs, filtered_meta

        logger.warning(
            "Company filter '%s' returned no matches in metadata; falling back to unfiltered results.",
            company_name,
        )
        return docs, metadata

    def _model_descriptor(self) -> Dict[str, Any]:
        resolved_path = Path(os.getenv("LLAMA_MODEL_PATH") or DEFAULT_MODEL_PATH).expanduser()
        return {
            "llama_model": str(resolved_path),
            "mercury_enabled": bool(INCEPTION_API_KEY),
            "hf_model": HF_MODEL_NAME,
        }


def _build_arg_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(description="Run the FrontShiftAI RAG pipeline.")
    parser.add_argument("query", help="Natural language question to answer.")
    parser.add_argument("--company-name", help="Optional company filter.", default=None)
    parser.add_argument(
        "--retriever",
        choices=sorted(RETRIEVER_REGISTRY.keys()),
        help="Retriever backend to use.",
    )
    parser.add_argument(
        "--top-k",
        type=int,
        help="Override the number of documents fetched by the retriever.",
    )
    parser.add_argument(
        "--enable-reranker",
        action="store_true",
        help="Enable the configured reranker (defaults to two_stage).",
    )
    parser.add_argument(
        "--reranker",
        choices=sorted(RERANKER_REGISTRY.keys()),
        help="Choose a reranker strategy.",
    )
    parser.add_argument(
        "--stream",
        action="store_true",
        help="Stream tokens instead of waiting for the full response.",
    )
    parser.add_argument(
        "--template-key",
        help="Prompt template identifier defined in prompt_templates.py.",
    )
    return parser


def _stream_to_stdout(stream: Iterable[str]) -> str:
    """Utility for the CLI that prints tokens as they arrive."""

    collected: List[str] = []
    for token in stream:
        print(token, end="", flush=True)
        collected.append(token)
    print()
    return "".join(collected).strip()


def main() -> None:  # pragma: no cover - CLI glue
    parser = _build_arg_parser()
    args = parser.parse_args()

    pipeline = RAGPipeline()
    reranker_enabled = args.enable_reranker or bool(args.reranker)
    result = pipeline.run(
        args.query,
        company_name=args.company_name,
        retriever=args.retriever,
        top_k=args.top_k,
        reranker_enabled=reranker_enabled if args.reranker or args.enable_reranker else None,
        reranker_strategy=args.reranker,
        template_key=args.template_key,
        stream=args.stream,
    )

    if result.is_stream():
        output = (
            _stream_to_stdout(result.answer)
            if inspect.isgenerator(result.answer)
            else str(result.answer)
        )
    else:
        output = (
            "".join(result.answer)
            if not isinstance(result.answer, str)
            else result.answer
        )
        print(output)

    if result.timings:
        print("\nTimings (s):")
        for name, value in result.timings.items():
            print(f"- {name}: {value:.3f}")

    print("\nSources:")
    for meta in result.metadata:
        company = meta.get("company", "unknown")
        filename = meta.get("filename", "unknown")
        chunk = meta.get("chunk_id", "?")
        print(f"- {company} | {filename} (chunk {chunk})")


if __name__ == "__main__":  # pragma: no cover - CLI entrypoint
    main()


__all__ = ["RAGPipeline", "PipelineConfig", "PipelineResult", "load_pipeline_config"]

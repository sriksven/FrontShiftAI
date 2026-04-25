"""Health check endpoint for Cloud Run"""
from fastapi import APIRouter, HTTPException, Response
from fastapi.responses import JSONResponse
from sqlalchemy.orm import Session
from sqlalchemy import text
from db.connection import SessionLocal

router = APIRouter()

@router.get("/health")
async def health_check():
    """Health check for Cloud Run and monitoring"""
    try:
        with SessionLocal() as db:
            db.execute(text("SELECT 1"))

        return {
            "status": "healthy",
            "database": "connected",
            "service": "backend"
        }
    except Exception as e:
        return JSONResponse(
            status_code=503,
            content={
                "status": "unhealthy",
                "database": "disconnected",
                "error": str(e),
                "service": "backend"
            }
        )


@router.get("/health/ready")
async def readiness_check():
    """
    Readiness check - verifies that RAG models are loaded and ready
    Use this to check if warmup completed successfully
    """
    health_status = {
        "status": "ready",
        "service": "backend",
        "models": {}
    }

    try:
        with SessionLocal() as db:
            db.execute(text("SELECT 1"))
        health_status["database"] = "connected"
    except Exception as e:
        health_status["database"] = f"error: {str(e)}"
        health_status["status"] = "not_ready"

    # Check if embedding model is loaded
    try:
        from chat_pipeline.rag.data_loader import _embedding_function
        embedding_fn = _embedding_function()
        health_status["models"]["embedding"] = "loaded"
    except Exception as e:
        health_status["models"]["embedding"] = f"error: {str(e)}"
        health_status["status"] = "not_ready"

    # Check if ChromaDB collection is loaded
    try:
        from chat_pipeline.rag.data_loader import get_collection
        collection = get_collection()
        doc_count = collection.count()
        health_status["models"]["chromadb"] = f"loaded ({doc_count} docs)"
    except Exception as e:
        health_status["models"]["chromadb"] = f"error: {str(e)}"
        health_status["status"] = "not_ready"

    # Check if reranker is loaded (if enabled)
    try:
        from chat_pipeline.rag.config_manager import load_rag_config
        rag_config = load_rag_config()
        if rag_config.get("pipeline", {}).get("reranker", {}).get("enabled", False):
            from chat_pipeline.rag.reranker import _get_cross_encoder
            reranker = _get_cross_encoder()
            health_status["models"]["reranker"] = "loaded"
        else:
            health_status["models"]["reranker"] = "disabled"
    except Exception as e:
        health_status["models"]["reranker"] = f"error: {str(e)}"
        # Don't mark as not_ready if reranker fails - it's optional

    if health_status["status"] == "not_ready":
        return JSONResponse(status_code=503, content=health_status)

    return health_status
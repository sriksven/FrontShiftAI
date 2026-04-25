"""Website Extraction Agent Tools - Brave Search API Client"""
import os
import re
import logging
import time
from typing import Dict, List, Optional, Tuple
from urllib.parse import urlparse
import requests
from sqlalchemy.orm import Session
from db.models import Company
from utils.resilience import CircuitOpenError, get_policy, resilient

logger = logging.getLogger(__name__)

BRAVE_API_KEY = os.getenv("BRAVE_API_KEY")
BRAVE_API_URL = "https://api.search.brave.com/res/v1/web/search"
# Timeout now comes from the external_search policy (5s). The env-var override
# still exists but is advisory — keep it for per-environment tuning but note
# that the policy governs retry/backoff semantics.
BRAVE_TIMEOUT = int(os.getenv("BRAVE_SEARCH_TIMEOUT", str(int(get_policy("external_search").timeout_s))))
BRAVE_MAX_RESULTS = int(os.getenv("BRAVE_MAX_RESULTS", "10"))
CONFIDENCE_THRESHOLD = float(os.getenv("WEBSITE_AGENT_CONFIDENCE_THRESHOLD", "0.5"))


@resilient(policy="external_search", breaker_key="brave")
def _brave_get(url: str, headers: dict, params: dict, timeout: float) -> requests.Response:
    """Single outbound Brave HTTP call, wrapped by the external_search policy.

    Retry/backoff/breaker semantics are centralized — this function just does
    one request and lets @resilient handle the envelope.
    """
    response = requests.get(url, headers=headers, params=params, timeout=timeout)
    response.raise_for_status()
    return response


def brave_search(query: str, site_domain: Optional[str] = None) -> Tuple[List[dict], Optional[str]]:
    """Execute Brave Search API call"""
    if not BRAVE_API_KEY:
        return [], "BRAVE_API_KEY not configured"

    search_query = f"{query} site:{site_domain}" if site_domain else query

    headers = {
        "X-Subscription-Token": BRAVE_API_KEY,
        "Accept": "application/json"
    }
    params = {
        "q": search_query,
        "count": BRAVE_MAX_RESULTS,
        "result_filter": "web",
        "search_lang": "en",
        "text_decorations": "false"
    }

    try:
        start_time = time.time()
        response = _brave_get(BRAVE_API_URL, headers=headers, params=params, timeout=BRAVE_TIMEOUT)
        
        data = response.json()
        results = data.get("web", {}).get("results", [])
        
        parsed_results = []
        for r in results:
            parsed_results.append({
                "title": r.get("title", ""),
                "url": r.get("url", ""),
                "description": r.get("description", ""),
                "extra_snippets": r.get("extra_snippets", []),
                "age": r.get("age", "")
            })
        
        logger.info(f"Brave search completed in {(time.time() - start_time)*1000:.0f}ms, {len(parsed_results)} results")
        return parsed_results, None
        
    except CircuitOpenError as e:
        logger.warning(f"Brave circuit breaker open: {e}")
        return [], "Search service temporarily unavailable"
    except requests.Timeout:
        return [], "Search timeout"
    except requests.RequestException as e:
        logger.error(f"Brave API error: {e}")
        return [], str(e)


def get_company_domain(db: Session, company_name: str) -> Tuple[Optional[str], Optional[str]]:
    """Get company URL and extract domain from database"""
    company = db.query(Company).filter(Company.name == company_name).first()
    
    if not company or not company.url:
        return None, None
    
    try:
        parsed = urlparse(company.url)
        domain = parsed.netloc or parsed.path.split('/')[0]
        domain = domain.replace("www.", "")
        return company.url, domain
    except Exception:
        return company.url, None


def score_result_relevance(result: dict, keywords: List[str], topic: str) -> float:
    """Score a search result's relevance"""
    title = result.get("title", "").lower()
    description = result.get("description", "").lower()
    url = result.get("url", "").lower()
    extra = " ".join(result.get("extra_snippets", [])).lower()
    
    combined_text = f"{title} {description} {extra}"
    
    # Keyword match score (40%)
    keyword_matches = sum(1 for kw in keywords if kw.lower() in combined_text)
    keyword_score = min(keyword_matches / max(len(keywords), 1), 1.0)
    
    # Title relevance (25%)
    topic_lower = topic.lower()
    title_score = 1.0 if topic_lower in title else (0.5 if any(w in title for w in topic_lower.split()) else 0.0)
    
    # Snippet quality (25%)
    snippet_score = 0.0
    if len(description) > 100:
        snippet_score += 0.4
    if result.get("extra_snippets"):
        snippet_score += 0.3
    if any(p in combined_text for p in ["hours", "phone", "address", "email", "contact", "location"]):
        snippet_score += 0.3
    snippet_score = min(snippet_score, 1.0)
    
    # Page type boost (10%)
    page_boost = 0.5
    if any(p in url for p in ["/contact", "/about", "/faq", "/hours", "/location"]):
        page_boost = 1.0
    elif "/blog" in url or "/news" in url:
        page_boost = 0.2
    
    final_score = (0.40 * keyword_score + 0.25 * title_score + 0.25 * snippet_score + 0.10 * page_boost)
    return round(min(1.0, max(0.0, final_score)), 3)


def rank_results(results: List[dict], keywords: List[str], topic: str) -> List[dict]:
    """Rank results by relevance score"""
    scored = []
    for r in results:
        score = score_result_relevance(r, keywords, topic)
        scored.append({**r, "relevance_score": score})
    
    return sorted(scored, key=lambda x: x["relevance_score"], reverse=True)


def extract_domain_from_url(url: str) -> str:
    """Extract clean domain from URL"""
    try:
        parsed = urlparse(url)
        domain = parsed.netloc or parsed.path.split('/')[0]
        return domain.replace("www.", "")
    except Exception:
        return url


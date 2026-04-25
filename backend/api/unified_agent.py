"""
Unified Agent - Handles RAG, PTO, HR Tickets, and Website Extraction
WITH PERSISTENT CHAT STORAGE AND MONITORING
"""
from fastapi import APIRouter, Depends, HTTPException, Header, Request
from sqlalchemy.orm import Session
from typing import Dict, Any, Optional, List
import uuid
import logging
import time
from datetime import datetime

from db.connection import get_db
from db.models import Conversation, Message, User
from api.auth import get_current_user
from api.idempotency import IdempotencyGuard, idempotency_guard
from api.rag import rag_query
from agents.pto.agent import PTOAgent
from agents.hr_ticket.agent import HRTicketAgent
from agents.website_extraction.agent import WebsiteExtractionAgent
from agents.utils.llm_client import AgentLLMClient, get_llm_client
from pydantic import BaseModel
from schemas.rag import RAGQueryRequest
from monitoring.production_logger import production_monitor
import json
from typing import List, Optional

router = APIRouter(prefix="/api/chat", tags=["Unified Agent"])


class ChatRequest(BaseModel):
    message: str
    conversation_id: Optional[str] = None


class ChatResponse(BaseModel):
    response: str
    agent_used: str
    conversation_id: str
    metadata: dict = {}


class ConversationResponse(BaseModel):
    id: str
    title: str
    created_at: str
    updated_at: str


class MessageResponse(BaseModel):
    id: str
    role: str
    content: str
    agent_type: Optional[str]
    created_at: str


def detect_intent(message: str) -> dict:
    """
    Detect user intent and which agent should handle it.
    Returns: {'agent': 'rag'|'pto'|'hr_ticket'|'website_extraction', 'confidence': 'high'|'low'}
    """
    message_lower = message.lower()
    
    # Website extraction keywords (direct request)
    website_keywords = [
        'company website', 'on their website', 'check website', 'look up online',
        'find online', 'from the website', 'website says', 'their site',
        'official site', 'company site', 'search website', 'on the web'
    ]
    
    for keyword in website_keywords:
        if keyword in message_lower:
            return {'agent': 'website_extraction', 'confidence': 'high'}
    
    # High-confidence PTO keywords
    pto_strong = [
        'request pto', 'request leave', 'request time off', 'request vacation',
        'book pto', 'book leave', 'book vacation', 'take time off',
        'need time off', 'need leave', 'need vacation', 'days off',
        'pto balance', 'leave balance', 'how many days', 'available days',
        'remaining days', 'check balance', 'my balance', 'check my balance',
        'i need leave', 'i need time off', 'i need pto', 'i want to take',
        'can i take', 'taking leave', 'taking time off'
    ]
    
    # High-confidence HR Ticket keywords
    hr_strong = [
        'schedule meeting with hr', 'meet with hr', 'talk to hr', 'speak to hr',
        'hr meeting', 'hr appointment', 'meeting with human resources',
        'schedule hr', 'book hr meeting', 'create ticket', 'hr ticket',
        'open ticket', 'submit ticket', 'paycheck issue', 'payroll problem',
        'meet hr', 'discuss with hr', 'contact hr', 'hr help',
        'insurance meeting', 'benefits meeting', 'schedule with hr'
    ]
    
    # Check for strong matches
    for keyword in pto_strong:
        if keyword in message_lower:
            return {'agent': 'pto', 'confidence': 'high'}
    
    for keyword in hr_strong:
        if keyword in message_lower:
            return {'agent': 'hr_ticket', 'confidence': 'high'}
    
    # Use LLM for ambiguous cases
    try:
        llm_client = get_llm_client()
        
        system_prompt = """Analyze the message and determine intent. Respond with JSON only.

**PTO Agent** - User wants to ACTION:
- Request/book time off, vacation, leave, PTO
- Check their PTO/leave balance or available days
- Examples: "I need 3 days off", "request leave for next week", "how many days do I have left"

**HR Ticket Agent** - User wants to SCHEDULE/MEET:
- Schedule a meeting with HR
- Discuss benefits, insurance, payroll issues with HR (needs meeting)
- Report workplace issues requiring HR intervention
- Create a support ticket
- Examples: "schedule meeting with HR", "discuss my insurance with HR", "paycheck problem, need to talk to HR"

**RAG Agent** - User wants to LEARN/KNOW:
- Learn about company policies (remote work, dress code, procedures)
- Ask general informational questions about the handbook
- Get information without taking action or scheduling
- Examples: "what is the PTO policy", "tell me about benefits", "what are the company holidays", "how does remote work work"

IMPORTANT: 
- Questions ABOUT policies = RAG
- REQUESTS for action = PTO or HR Ticket
- "What is the PTO policy?" = RAG
- "I need PTO" = PTO Agent
- "Schedule meeting about benefits" = HR Ticket

JSON format:
{
    "agent": "pto" or "hr_ticket" or "rag",
    "reasoning": "brief explanation"
}"""

        messages = [
            {"role": "system", "content": system_prompt},
            {"role": "user", "content": message}
        ]
        
        response = llm_client.chat(messages, json_mode=True, temperature=0.2)
        result = json.loads(response)
        return {'agent': result.get('agent', 'rag'), 'confidence': 'medium'}
        
    except Exception as e:
        print(f"Intent detection failed: {e}")
        # Smart fallback based on question words
        question_words = ['what', 'how', 'why', 'when', 'where', 'who', 'which', 
                         'tell me', 'explain', 'describe', 'define']
        
        if any(word in message_lower for word in question_words):
            return {'agent': 'rag', 'confidence': 'low'}
        else:
            # If no question words, likely an action request
            return {'agent': 'hr_ticket', 'confidence': 'low'}


@router.post("/message", response_model=ChatResponse)
async def unified_chat(
    request: ChatRequest,
    current_user: dict = Depends(get_current_user),
    db: Session = Depends(get_db),
    idem: IdempotencyGuard = Depends(idempotency_guard),
):
    """
    Unified chat endpoint - intelligently routes to RAG, PTO, or HR Ticket agents
    AND saves to database WITH MONITORING
    """
    message = request.message
    conversation_id = request.conversation_id
    company = current_user["company"]

    # Idempotency: if the client replays with the same key, return the cached
    # response and skip writing duplicate Conversation/Message rows or
    # duplicate PTO/HR tickets downstream.
    cached = idem.lookup(db, company, endpoint="/api/chat/message")
    if cached is not None:
        return cached

    def _respond(**kwargs) -> ChatResponse:
        """Build ChatResponse and persist it under the idempotency key (if any)."""
        resp = ChatResponse(**kwargs)
        idem.store(
            db, company, endpoint="/api/chat/message",
            status_code=200, body=resp.model_dump(),
        )
        return resp

    # Start timing
    start_time = time.time()
    
    # Create or get conversation
    if not conversation_id:
        conversation_id = str(uuid.uuid4())
        title = message[:50] + ('...' if len(message) > 50 else '')
        
        new_conversation = Conversation(
            id=conversation_id,
            email=current_user["email"],
            company=company,
            title=title
        )
        db.add(new_conversation)
        db.commit()
    
    # Save user message
    user_message = Message(
        id=str(uuid.uuid4()),
        conversation_id=conversation_id,
        role='user',
        content=message,
        agent_type=None
    )
    db.add(user_message)
    db.commit()
    
    # Detect intent
    intent = detect_intent(message)
    agent_type = intent['agent']
    
    print(f"🤖 Routing to {agent_type} agent (confidence: {intent['confidence']})")
    
    # Track agent selection
    agent_start_time = time.time()
    success = False
    
    try:
        if agent_type == 'pto':
            # Use PTO Agent
            pto_agent = PTOAgent(db)
            result = await pto_agent.execute(
                user_email=current_user["email"],
                company=company,
                message=message
            )
            
            success = True
            
            # Log agent execution
            execution_time_ms = (time.time() - agent_start_time) * 1000
            production_monitor.log_agent_execution(
                agent_name="pto",
                execution_time_ms=execution_time_ms,
                success=True,
                company_id=company
            )
            
            # Save assistant message
            assistant_message = Message(
                id=str(uuid.uuid4()),
                conversation_id=conversation_id,
                role='assistant',
                content=result["response"],
                agent_type='pto',
                message_metadata=json.dumps({
                    "request_created": result.get("request_created", False),
                    "request_id": result.get("request_id"),
                    "balance_info": result.get("balance_info")
                })
            )
            db.add(assistant_message)
            db.commit()
            
            return _respond(
                response=result["response"],
                agent_used="pto",
                conversation_id=conversation_id,
                metadata={
                    "request_created": result.get("request_created", False),
                    "request_id": result.get("request_id"),
                    "balance_info": result.get("balance_info")
                }
            )
            
        elif agent_type == 'hr_ticket':
            # Use HR Ticket Agent
            hr_agent = HRTicketAgent()
            result = await hr_agent.process_message(
                user_email=current_user["email"],
                company=company,
                message=message,
                db=db
            )
            
            success = True
            
            # Log agent execution
            execution_time_ms = (time.time() - agent_start_time) * 1000
            production_monitor.log_agent_execution(
                agent_name="hr_ticket",
                execution_time_ms=execution_time_ms,
                success=True,
                company_id=company
            )
            
            # Save assistant message
            assistant_message = Message(
                id=str(uuid.uuid4()),
                conversation_id=conversation_id,
                role='assistant',
                content=result["response"],
                agent_type='hr_ticket',
                message_metadata=json.dumps({
                    "ticket_created": result.get("ticket_created", False),
                    "ticket_id": result.get("ticket_id"),
                    "queue_position": result.get("queue_position")
                })
            )
            db.add(assistant_message)
            db.commit()
            
            return _respond(
                response=result["response"],
                agent_used="hr_ticket",
                conversation_id=conversation_id,
                metadata={
                    "ticket_created": result.get("ticket_created", False),
                    "ticket_id": result.get("ticket_id"),
                    "queue_position": result.get("queue_position")
                }
            )
            
        elif agent_type == 'website_extraction':
            website_agent = WebsiteExtractionAgent(db)
            result = await website_agent.execute(
                user_email=current_user["email"],
                company=company,
                message=message,
                triggered_by="direct"
            )
            
            success = True
            
            # Log agent execution
            execution_time_ms = (time.time() - agent_start_time) * 1000
            production_monitor.log_agent_execution(
                agent_name="website_extraction",
                execution_time_ms=execution_time_ms,
                success=True,
                company_id=company
            )
            
            assistant_message = Message(
                id=str(uuid.uuid4()),
                conversation_id=conversation_id,
                role='assistant',
                content=result["response"],
                agent_type='website_extraction',
                message_metadata=json.dumps({
                    "found_answer": result.get("found_answer", False),
                    "source_urls": result.get("source_urls", []),
                    "confidence": result.get("confidence", 0.0),
                    "suggest_hr_ticket": result.get("suggest_hr_ticket", False)
                })
            )
            db.add(assistant_message)
            db.commit()
            
            return _respond(
                response=result["response"],
                agent_used="website_extraction",
                conversation_id=conversation_id,
                metadata={
                    "found_answer": result.get("found_answer", False),
                    "source_urls": result.get("source_urls", []),
                    "suggest_hr_ticket": result.get("suggest_hr_ticket", False)
                }
            )
            
        else:  # rag
            rag_request = RAGQueryRequest(
                query=message,
                top_k=3
            )
            
            rag_result = await rag_query(rag_request, current_user)
            
            # Check if RAG found nothing - fallback to website extraction
            no_context_phrases = [
                "no relevant context",
                "couldn't find",
                "not found in the",
                "no information available",
                "isn't available in the provided"
            ]
            
            rag_failed = any(phrase in rag_result.answer.lower() for phrase in no_context_phrases)
            
            if rag_failed:
                print(f"🔄 RAG found nothing, trying website extraction...")
                website_agent = WebsiteExtractionAgent(db)
                website_result = await website_agent.execute(
                    user_email=current_user["email"],
                    company=company,
                    message=message,
                    triggered_by="rag_fallback"
                )
                
                success = True
                
                # Log website extraction (fallback)
                execution_time_ms = (time.time() - agent_start_time) * 1000
                production_monitor.log_agent_execution(
                    agent_name="website_extraction_fallback",
                    execution_time_ms=execution_time_ms,
                    success=True,
                    company_id=company
                )
                
                assistant_message = Message(
                    id=str(uuid.uuid4()),
                    conversation_id=conversation_id,
                    role='assistant',
                    content=website_result["response"],
                    agent_type='website_extraction',
                    message_metadata=json.dumps({
                        "found_answer": website_result.get("found_answer", False),
                        "source_urls": website_result.get("source_urls", []),
                        "triggered_by": "rag_fallback",
                        "suggest_hr_ticket": website_result.get("suggest_hr_ticket", False)
                    })
                )
                db.add(assistant_message)
                db.commit()
                
                return _respond(
                    response=website_result["response"],
                    agent_used="website_extraction",
                    conversation_id=conversation_id,
                    metadata={
                        "found_answer": website_result.get("found_answer", False),
                        "source_urls": website_result.get("source_urls", []),
                        "triggered_by": "rag_fallback",
                        "suggest_hr_ticket": website_result.get("suggest_hr_ticket", False)
                    }
                )
            
            success = True
            
            # Log RAG execution
            execution_time_ms = (time.time() - agent_start_time) * 1000
            production_monitor.log_agent_execution(
                agent_name="rag",
                execution_time_ms=execution_time_ms,
                success=True,
                company_id=company
            )
            
            assistant_message = Message(
                id=str(uuid.uuid4()),
                conversation_id=conversation_id,
                role='assistant',
                content=rag_result.answer,
                agent_type='rag',
                message_metadata=json.dumps({
                    "sources": rag_result.sources,
                    "company": rag_result.company
                })
            )
            db.add(assistant_message)
            db.commit()
            
            return _respond(
                response=rag_result.answer,
                agent_used="rag",
                conversation_id=conversation_id,
                metadata={
                    "sources": rag_result.sources,
                    "company": rag_result.company
                }
            )
            
    except Exception as e:
        print(f"❌ Error in unified agent: {e}")
        import traceback
        traceback.print_exc()
        
        # Log failure
        execution_time_ms = (time.time() - agent_start_time) * 1000
        production_monitor.log_agent_execution(
            agent_name=agent_type,
            execution_time_ms=execution_time_ms,
            success=False,
            company_id=company
        )
        
        # Save error message (graceful)
        error_message = Message(
            id=str(uuid.uuid4()),
            conversation_id=conversation_id,
            role='assistant',
            content="I apologize, but I encountered an internal error while processing your request. Our team has been notified.",
            agent_type='error'
        )
        db.add(error_message)
        db.commit()
        
        # Don't cache error responses — a retry with the same key should get
        # a real attempt, not the cached apology.
        return ChatResponse(
            response="I apologize, but I encountered an internal error while processing your request. Our team has been notified.",
            agent_used="error",
            conversation_id=conversation_id,
            metadata={"error_type": "internal_server_error"}
        )


@router.get("/conversations", response_model=List[ConversationResponse])
async def get_conversations(
    current_user: dict = Depends(get_current_user),
    db: Session = Depends(get_db)
):
    """Get all conversations for current user"""
    conversations = db.query(Conversation).filter(
        Conversation.email == current_user["email"]
    ).order_by(Conversation.updated_at.desc()).all()
    
    return [
        ConversationResponse(
            id=c.id,
            title=c.title,
            created_at=c.created_at.isoformat(),
            updated_at=c.updated_at.isoformat()
        )
        for c in conversations
    ]


@router.get("/conversations/{conversation_id}/messages", response_model=List[MessageResponse])
async def get_conversation_messages(
    conversation_id: str,
    current_user: dict = Depends(get_current_user),
    db: Session = Depends(get_db)
):
    """Get all messages for a conversation"""
    # Verify conversation belongs to user
    conversation = db.query(Conversation).filter(
        Conversation.id == conversation_id,
        Conversation.email == current_user["email"]
    ).first()
    
    if not conversation:
        return []
    
    messages = db.query(Message).filter(
        Message.conversation_id == conversation_id
    ).order_by(Message.created_at.asc()).all()
    
    return [
        MessageResponse(
            id=m.id,
            role=m.role,
            content=m.content,
            agent_type=m.agent_type,
            created_at=m.created_at.isoformat()
        )
        for m in messages
    ]


@router.delete("/conversations/{conversation_id}")
async def delete_conversation(
    conversation_id: str,
    current_user: dict = Depends(get_current_user),
    db: Session = Depends(get_db)
):
    """Delete a conversation and all its messages"""
    # Verify conversation belongs to user
    conversation = db.query(Conversation).filter(
        Conversation.id == conversation_id,
        Conversation.email == current_user["email"]
    ).first()
    
    if not conversation:
        return {"message": "Conversation not found"}
    
    # Delete messages first
    db.query(Message).filter(Message.conversation_id == conversation_id).delete()
    
    # Delete conversation
    db.delete(conversation)
    db.commit()
    
    return {"message": "Conversation deleted"}


@router.get("/health")
def health_check():
    """Health check for unified agent"""
    return {
        "status": "ok", 
        "message": "Unified agent is running",
        "agents": ["rag", "pto", "hr_ticket", "website_extraction"]
    }
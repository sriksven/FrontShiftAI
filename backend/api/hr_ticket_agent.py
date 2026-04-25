"""
HR Ticket API endpoints with Monitoring
"""
from fastapi import APIRouter, Depends, HTTPException, Query
from sqlalchemy.orm import Session
from typing import Optional, List
from datetime import datetime
import time  # ADD THIS

from db.connection import get_db
from db.models import HRTicket, TicketStatus, TicketCategory, Urgency, User, UserRole
from schemas.hr_ticket import (
    HRTicketChatRequest,
    HRTicketChatResponse,
    HRTicketResponse,
    HRTicketListResponse,
    PickTicketRequest,
    ScheduleMeetingRequest,
    ResolveTicketRequest,
    AddNoteRequest,
    TicketStatsResponse,
    SimpleResponse
)
from agents.hr_ticket import get_hr_ticket_agent
from agents.hr_ticket.tools import (
    get_ticket_by_id,
    get_user_tickets,
    is_company_admin,
    get_ticket_stats
)
from api.auth import get_current_user
from api.idempotency import IdempotencyGuard, idempotency_guard
from monitoring.production_logger import production_monitor  # ADD THIS

router = APIRouter(prefix="/api/hr-tickets", tags=["HR Tickets"])


# ==========================================
# USER ENDPOINTS
# ==========================================

@router.post("/chat", response_model=HRTicketChatResponse)
async def create_ticket_via_chat(
    request: HRTicketChatRequest,
    current_user: dict = Depends(get_current_user),
    db: Session = Depends(get_db),
    idem: IdempotencyGuard = Depends(idempotency_guard),
):
    """
    Create an HR ticket via chat interface.
    User sends a natural language message, agent parses and creates ticket.
    """
    # Idempotency: replayed voice tool calls (same key) return the cached response
    # without creating a duplicate HRTicket.
    cached = idem.lookup(db, current_user["company"], endpoint="/api/hr-tickets/chat")
    if cached is not None:
        return cached

    start_time = time.time()  # ADD THIS
    success = False  # ADD THIS

    try:
        agent = get_hr_ticket_agent()

        result = await agent.process_message(
            user_email=current_user["email"],
            company=current_user["company"],
            message=request.message,
            db=db
        )

        success = True  # ADD THIS

        # Log agent execution
        execution_time_ms = (time.time() - start_time) * 1000
        production_monitor.log_agent_execution(
            agent_name="hr_ticket",
            execution_time_ms=execution_time_ms,
            success=True,
            company_id=current_user["company"]
        )

        # Log ticket creation metric
        if result["ticket_created"] and production_monitor.run:
            production_monitor.run.log({
                "business/hr_ticket_created": 1,
                "business/company": current_user["company"],
                "timestamp": time.time()
            })

        # Get ticket details if created
        ticket_details = None
        if result["ticket_created"] and result["ticket_id"]:
            ticket = get_ticket_by_id(db, result["ticket_id"], current_user["company"])
            if ticket:
                ticket_details = HRTicketResponse.model_validate(ticket)

        response = HRTicketChatResponse(
            response=result["response"],
            ticket_created=result["ticket_created"],
            ticket_id=result.get("ticket_id"),
            queue_position=result.get("queue_position"),
            ticket_details=ticket_details
        )
        idem.store(
            db,
            current_user["company"],
            endpoint="/api/hr-tickets/chat",
            status_code=200,
            body=response.model_dump(),
        )
        return response

    except Exception as e:
        # Log failure
        execution_time_ms = (time.time() - start_time) * 1000
        production_monitor.log_agent_execution(
            agent_name="hr_ticket",
            execution_time_ms=execution_time_ms,
            success=False,
            company_id=current_user["company"]
        )
        raise


@router.get("/my-tickets", response_model=HRTicketListResponse)
def get_my_tickets(
    current_user: dict = Depends(get_current_user),
    db: Session = Depends(get_db)
):
    """Get all tickets for the current user"""
    start_time = time.time()  # ADD THIS
    
    tickets = get_user_tickets(db, current_user["email"], current_user["company"])
    
    # Log query performance
    query_time_ms = (time.time() - start_time) * 1000
    production_monitor.log_database_query(
        query_type="hr_tickets_user_list",
        execution_time_ms=query_time_ms,
        rows_affected=len(tickets)
    )
    
    return HRTicketListResponse(
        tickets=[HRTicketResponse.model_validate(ticket) for ticket in tickets],
        total_count=len(tickets)
    )


@router.get("/{ticket_id}", response_model=HRTicketResponse)
def get_ticket_details(
    ticket_id: str,
    current_user: dict = Depends(get_current_user),
    db: Session = Depends(get_db)
):
    """Get details of a specific ticket"""
    start_time = time.time()  # ADD THIS
    
    ticket = get_ticket_by_id(db, ticket_id, current_user["company"])
    
    if not ticket:
        raise HTTPException(status_code=404, detail="Ticket not found")
    
    # Users can only see their own tickets
    if current_user["role"] == "user" and ticket.email != current_user["email"]:
        raise HTTPException(status_code=403, detail="Not authorized to view this ticket")
    
    # Log query performance
    query_time_ms = (time.time() - start_time) * 1000
    production_monitor.log_database_query(
        query_type="hr_ticket_detail",
        execution_time_ms=query_time_ms
    )
    
    return HRTicketResponse.model_validate(ticket)


@router.delete("/{ticket_id}", response_model=SimpleResponse)
def cancel_ticket(
    ticket_id: str,
    current_user: dict = Depends(get_current_user),
    db: Session = Depends(get_db)
):
    """Cancel a ticket (only if pending or scheduled)"""
    start_time = time.time()  # ADD THIS
    
    ticket = get_ticket_by_id(db, ticket_id, current_user["company"])
    
    if not ticket:
        raise HTTPException(status_code=404, detail="Ticket not found")
    
    # Only ticket owner can cancel
    if ticket.email != current_user["email"]:
        raise HTTPException(status_code=403, detail="Not authorized to cancel this ticket")
    
    # Can only cancel pending or scheduled tickets
    if ticket.status not in [TicketStatus.PENDING, TicketStatus.SCHEDULED]:
        raise HTTPException(
            status_code=400,
            detail=f"Cannot cancel ticket with status: {ticket.status}"
        )
    
    # Mark as closed
    ticket.status = TicketStatus.CLOSED
    ticket.resolved_at = datetime.utcnow()
    ticket.resolution_notes = "Cancelled by user"
    
    db.commit()
    
    # Log cancellation metric
    execution_time_ms = (time.time() - start_time) * 1000
    if production_monitor.run:
        production_monitor.run.log({
            "business/hr_ticket_cancelled": 1,
            "business/company": current_user["company"],
            "business/cancellation_time_ms": execution_time_ms,
            "timestamp": time.time()
        })
    
    return SimpleResponse(
        message="Ticket cancelled successfully",
        ticket_id=ticket_id
    )


# ==========================================
# ADMIN ENDPOINTS
# ==========================================

@router.get("/admin/queue", response_model=HRTicketListResponse)
def get_ticket_queue(
    status_filter: Optional[TicketStatus] = Query(None),
    category_filter: Optional[TicketCategory] = Query(None),
    urgency_filter: Optional[Urgency] = Query(None),
    sort_by: str = Query("created_at", regex="^(created_at|urgency|category)$"),
    current_user: dict = Depends(get_current_user),
    db: Session = Depends(get_db)
):
    """
    Get ticket queue for admin dashboard.
    Supports filtering and sorting.
    """
    # Only company admins can access
    if current_user["role"] != "company_admin":
        raise HTTPException(status_code=403, detail="Admin access required")
    
    start_time = time.time()  # ADD THIS
    
    # Build query
    query = db.query(HRTicket).filter(HRTicket.company == current_user["company"])
    
    # Apply filters
    if status_filter:
        query = query.filter(HRTicket.status == status_filter)
    
    if category_filter:
        query = query.filter(HRTicket.category == category_filter)
    
    if urgency_filter:
        query = query.filter(HRTicket.urgency == urgency_filter)
    
    # Apply sorting
    if sort_by == "urgency":
        # Urgent first, then by created_at
        query = query.order_by(HRTicket.urgency.desc(), HRTicket.created_at.asc())
    elif sort_by == "category":
        query = query.order_by(HRTicket.category, HRTicket.created_at.asc())
    else:  # created_at (default)
        query = query.order_by(HRTicket.created_at.asc())
    
    tickets = query.all()
    
    # Log admin queue query
    query_time_ms = (time.time() - start_time) * 1000
    production_monitor.log_database_query(
        query_type="admin_hr_ticket_queue",
        execution_time_ms=query_time_ms,
        rows_affected=len(tickets)
    )
    
    return HRTicketListResponse(
        tickets=[HRTicketResponse.model_validate(ticket) for ticket in tickets],
        total_count=len(tickets)
    )


@router.post("/admin/pick-ticket", response_model=SimpleResponse)
def pick_ticket(
    request: PickTicketRequest,
    current_user: dict = Depends(get_current_user),
    db: Session = Depends(get_db)
):
    """Admin picks up a ticket (assigns to themselves)"""
    if current_user["role"] != "company_admin":
        raise HTTPException(status_code=403, detail="Admin access required")
    
    start_time = time.time()  # ADD THIS
    
    ticket = get_ticket_by_id(db, request.ticket_id, current_user["company"])
    
    if not ticket:
        raise HTTPException(status_code=404, detail="Ticket not found")
    
    if ticket.status != TicketStatus.PENDING:
        raise HTTPException(
            status_code=400,
            detail=f"Can only pick up pending tickets. Current status: {ticket.status}"
        )
    
    # Assign to admin
    ticket.assigned_to = current_user["email"]
    ticket.status = TicketStatus.IN_PROGRESS
    ticket.picked_up_at = datetime.utcnow()
    
    db.commit()
    
    # Log ticket pickup
    execution_time_ms = (time.time() - start_time) * 1000
    if production_monitor.run:
        production_monitor.run.log({
            "business/hr_ticket_picked_up": 1,
            "business/company": current_user["company"],
            "business/pickup_time_ms": execution_time_ms,
            "timestamp": time.time()
        })
    
    return SimpleResponse(
        message="Ticket assigned to you",
        ticket_id=request.ticket_id
    )


@router.post("/admin/schedule-meeting", response_model=SimpleResponse)
def schedule_meeting(
    request: ScheduleMeetingRequest,
    current_user: dict = Depends(get_current_user),
    db: Session = Depends(get_db)
):
    """Admin schedules a meeting for a ticket"""
    if current_user["role"] != "company_admin":
        raise HTTPException(status_code=403, detail="Admin access required")
    
    start_time = time.time()  # ADD THIS
    
    ticket = get_ticket_by_id(db, request.ticket_id, current_user["company"])
    
    if not ticket:
        raise HTTPException(status_code=404, detail="Ticket not found")
    
    # Update ticket
    ticket.scheduled_datetime = request.scheduled_datetime
    ticket.meeting_link = request.meeting_link
    ticket.meeting_location = request.meeting_location
    ticket.status = TicketStatus.SCHEDULED
    
    if request.admin_notes:
        if ticket.admin_notes:
            ticket.admin_notes += f"\n\n[{datetime.utcnow().strftime('%Y-%m-%d %H:%M')}] {request.admin_notes}"
        else:
            ticket.admin_notes = request.admin_notes
    
    # Assign to admin if not already assigned
    if not ticket.assigned_to:
        ticket.assigned_to = current_user["email"]
    
    db.commit()
    
    # Log meeting scheduled
    execution_time_ms = (time.time() - start_time) * 1000
    if production_monitor.run:
        production_monitor.run.log({
            "business/hr_meeting_scheduled": 1,
            "business/company": current_user["company"],
            "business/schedule_time_ms": execution_time_ms,
            "timestamp": time.time()
        })
    
    return SimpleResponse(
        message="Meeting scheduled successfully",
        ticket_id=request.ticket_id
    )


@router.post("/admin/resolve", response_model=SimpleResponse)
def resolve_ticket(
    request: ResolveTicketRequest,
    current_user: dict = Depends(get_current_user),
    db: Session = Depends(get_db)
):
    """Admin resolves or closes a ticket"""
    if current_user["role"] != "company_admin":
        raise HTTPException(status_code=403, detail="Admin access required")
    
    start_time = time.time()  # ADD THIS
    
    ticket = get_ticket_by_id(db, request.ticket_id, current_user["company"])
    
    if not ticket:
        raise HTTPException(status_code=404, detail="Ticket not found")
    
    # Calculate resolution time if ticket was created
    resolution_time_hours = None
    if ticket.created_at:
        resolution_time_hours = (datetime.utcnow() - ticket.created_at).total_seconds() / 3600
    
    # Update ticket
    ticket.status = request.status
    ticket.resolved_at = datetime.utcnow()
    ticket.resolution_notes = request.resolution_notes
    
    # Assign to admin if not already assigned
    if not ticket.assigned_to:
        ticket.assigned_to = current_user["email"]
    
    db.commit()
    
    # Log ticket resolution
    execution_time_ms = (time.time() - start_time) * 1000
    metrics = {
        "business/hr_ticket_resolved": 1,
        "business/company": current_user["company"],
        "business/resolution_action_time_ms": execution_time_ms,
        "timestamp": time.time()
    }
    
    if resolution_time_hours:
        metrics["business/hr_ticket_resolution_time_hours"] = resolution_time_hours
    
    if production_monitor.run:
        production_monitor.run.log(metrics)
    
    return SimpleResponse(
        message=f"Ticket {request.status.value} successfully",
        ticket_id=request.ticket_id
    )


@router.post("/admin/add-note", response_model=SimpleResponse)
def add_note(
    request: AddNoteRequest,
    current_user: dict = Depends(get_current_user),
    db: Session = Depends(get_db)
):
    """Admin adds a note to a ticket"""
    if current_user["role"] != "company_admin":
        raise HTTPException(status_code=403, detail="Admin access required")
    
    ticket = get_ticket_by_id(db, request.ticket_id, current_user["company"])
    
    if not ticket:
        raise HTTPException(status_code=404, detail="Ticket not found")
    
    # Add timestamped note
    timestamp = datetime.utcnow().strftime('%Y-%m-%d %H:%M')
    new_note = f"[{timestamp}] {request.note}"
    
    if ticket.admin_notes:
        ticket.admin_notes += f"\n\n{new_note}"
    else:
        ticket.admin_notes = new_note
    
    db.commit()
    
    return SimpleResponse(
        message="Note added successfully",
        ticket_id=request.ticket_id
    )


@router.get("/admin/stats", response_model=TicketStatsResponse)
def get_admin_stats(
    current_user: dict = Depends(get_current_user),
    db: Session = Depends(get_db)
):
    """Get statistics for admin dashboard"""
    if current_user["role"] != "company_admin":
        raise HTTPException(status_code=403, detail="Admin access required")
    
    start_time = time.time()  # ADD THIS
    
    stats = get_ticket_stats(db, current_user["company"])
    
    # Log stats query
    query_time_ms = (time.time() - start_time) * 1000
    production_monitor.log_database_query(
        query_type="hr_ticket_stats",
        execution_time_ms=query_time_ms
    )
    
    return TicketStatsResponse(**stats)
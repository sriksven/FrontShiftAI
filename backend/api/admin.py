"""
Admin API endpoints
"""
from fastapi import APIRouter, HTTPException, Depends
from sqlalchemy.orm import Session
from schemas import CreateUserRequest, UpdatePasswordRequest, DeleteUserRequest, BulkCreateUserRequest
from services import (
    get_all_company_admins,
    get_users_by_company, add_user, delete_user, update_user_password
)
from api.auth import get_current_user
from db import get_db
from db.tenant_context import bypass_tenant_filter

router = APIRouter(prefix="/api/admin", tags=["Admin"])

def require_admin(current_user: dict, required_role: str = "company_admin"):
    """Check if user has admin permissions"""
    user_role = current_user.get("role")
    
    if required_role == "super_admin" and user_role != "super_admin":
        raise HTTPException(status_code=403, detail="Super admin access required")
    
    if required_role == "company_admin" and user_role not in ["super_admin", "company_admin"]:
        raise HTTPException(status_code=403, detail="Admin access required")

@router.get("/company-admins")
async def get_company_admins(
    current_user: dict = Depends(get_current_user),
    db: Session = Depends(get_db)
):
    """Get all company admins (Super Admin only)"""
    require_admin(current_user, "super_admin")

    from db.models import User, UserRole
    with bypass_tenant_filter(
        reason="list company admins across all tenants",
        actor=current_user["email"],
    ):
        admins = db.query(User).filter(User.role == UserRole.COMPANY_ADMIN).all()
    return {
        "admins": [
            {
                "email": admin.email,
                "name": admin.name,
                "company": admin.company,
                "created_at": admin.created_at.isoformat() if admin.created_at else None
            }
            for admin in admins
        ]
    }

@router.get("/all-companies")
async def get_all_companies_admin(
    current_user: dict = Depends(get_current_user),
    db: Session = Depends(get_db)
):
    """Get all companies (Super Admin only)"""
    require_admin(current_user, "super_admin")

    from db.models import Company
    # Company has no company column, so the listener wouldn't filter it anyway,
    # but wrap for consistency and audit-log traceability.
    with bypass_tenant_filter(
        reason="list all companies",
        actor=current_user["email"],
    ):
        companies = db.query(Company).all()

    return {
        "companies": [
            {
                "name": c.name,
                "domain": c.domain,
                "email_domain": c.email_domain,
                "url": c.url
            }
            for c in companies
        ]
    }

@router.post("/add-company-admin")
async def add_company_admin(
    request: CreateUserRequest,
    current_user: dict = Depends(get_current_user),
    db: Session = Depends(get_db)
):
    """Add a new company admin (Super Admin only)"""
    require_admin(current_user, "super_admin")
    
    success, message = add_user(
        email=request.email,
        password=request.password,
        company=request.company,
        name=request.name,
        role="company_admin",
        db=db
    )
    
    if not success:
        raise HTTPException(status_code=400, detail=message)
    
    return {"message": message, "email": request.email}

@router.delete("/delete-company-admin")
async def delete_company_admin(
    request: DeleteUserRequest,
    current_user: dict = Depends(get_current_user),
    db: Session = Depends(get_db)
):
    """Delete a company admin (Super Admin only)"""
    require_admin(current_user, "super_admin")
    
    success, message = delete_user(request.email, db)
    
    if not success:
        raise HTTPException(status_code=400, detail=message)
    
    return {"message": message}

@router.get("/company-users")
async def get_company_users(
    current_user: dict = Depends(get_current_user),
    db: Session = Depends(get_db)
):
    """Get all users in admin's company (Company Admin only)"""
    require_admin(current_user, "company_admin")
    
    company = current_user.get("company")
    if not company:
        raise HTTPException(status_code=400, detail="No company associated with this admin")
    
    users = get_users_by_company(company, db)
    return {"users": users, "company": company}

@router.post("/add-user")
async def add_company_user(
    request: CreateUserRequest,
    current_user: dict = Depends(get_current_user),
    db: Session = Depends(get_db)
):
    """Add a new user to admin's company (Company Admin only)"""
    require_admin(current_user, "company_admin")
    
    if current_user["role"] == "company_admin":
        company = current_user.get("company")
        if not company:
            raise HTTPException(status_code=400, detail="No company associated with this admin")
    else:
        # Super admin can specify company
        company = request.company
    
    success, message = add_user(
        email=request.email,
        password=request.password,
        company=company,
        name=request.name,
        role="user",
        db=db
    )
    
    if not success:
        raise HTTPException(status_code=400, detail=message)
    
    return {"message": message, "email": request.email}

@router.post("/bulk-add-users")
async def bulk_add_company_users(
    request: BulkCreateUserRequest,
    current_user: dict = Depends(get_current_user),
    db: Session = Depends(get_db)
):
    """Add multiple users to admin's company (Company Admin only)"""
    require_admin(current_user, "company_admin")
    
    if current_user["role"] == "company_admin":
        company = current_user.get("company")
        if not company:
            raise HTTPException(status_code=400, detail="No company associated with this admin")
    else:
        # Super admin fallback
        raise HTTPException(status_code=400, detail="Super admin bulk add not fully specified yet")

    results = {
        "added": 0,
        "failed": 0,
        "details": []
    }

    for user_req in request.users:
        success, message = add_user(
            email=user_req.email,
            password=user_req.password,
            company=company,
            name=user_req.name,
            role="user",
            db=db
        )
        
        if success:
            results["added"] += 1
            results["details"].append({"email": user_req.email, "status": "success"})
        else:
            results["failed"] += 1
            results["details"].append({"email": user_req.email, "status": "error", "message": message})
    
    return results

@router.delete("/delete-user")
async def delete_company_user(
    request: DeleteUserRequest,
    current_user: dict = Depends(get_current_user),
    db: Session = Depends(get_db)
):
    """Delete a user from admin's company (Company Admin only)"""
    require_admin(current_user, "company_admin")
    
    # Verify user belongs to admin's company
    if current_user["role"] == "company_admin":
        company = current_user.get("company")
        users = get_users_by_company(company, db)
        user_emails = [u["email"] for u in users]
        
        if request.email not in user_emails:
            raise HTTPException(
                status_code=403,
                detail="Cannot delete users from other companies"
            )
    
    success, message = delete_user(request.email, db)
    
    if not success:
        raise HTTPException(status_code=400, detail=message)
    
    return {"message": message}

@router.delete("/bulk-delete-users")
async def bulk_delete_company_users(
    company_name: str,
    current_user: dict = Depends(get_current_user),
    db: Session = Depends(get_db)
):
    """
    Delete ALL users from a company (Super Admin only).
    Does NOT delete the company itself.
    """
    require_admin(current_user, "super_admin")
    
    # Verify company exists (optional but good practice)
    from db.models import Company, User
    company_exists = db.query(Company).filter(Company.name == company_name).first()
    if not company_exists:
        raise HTTPException(status_code=404, detail=f"Company '{company_name}' not found")

    # Super-admin explicitly targeting another tenant — bypass the event listener.
    with bypass_tenant_filter(
        reason=f"bulk delete users for company={company_name}",
        actor=current_user["email"],
    ):
        users_to_delete = db.query(User).filter(User.company == company_name).all()
        count = len(users_to_delete)

        if count == 0:
            return {"message": f"No users found for company '{company_name}'", "count": 0}

        try:
            db.query(User).filter(User.company == company_name).delete(synchronize_session=False)
            db.commit()
        except Exception as e:
            db.rollback()
            raise HTTPException(status_code=500, detail=f"Failed to delete users: {str(e)}")

    return {
        "message": f"Successfully deleted {count} users from company '{company_name}'",
        "count": count,
        "company": company_name
    }

@router.put("/update-password")
async def update_password(
    request: UpdatePasswordRequest,
    current_user: dict = Depends(get_current_user),
    db: Session = Depends(get_db)
):
    """Update user password (Admin only)"""
    require_admin(current_user, "company_admin")
    
    success, message = update_user_password(request.email, request.new_password, db)
    
    if not success:
        raise HTTPException(status_code=400, detail=message)
    
    return {"message": message}

@router.get("/companies")
def get_companies(db: Session = Depends(get_db)):
    """Get list of all available companies"""
    from db.models import Company
    companies = db.query(Company).all()
    
    return {
        "companies": [
            {
                "name": c.name,
                "domain": c.domain,
                "email_domain": c.email_domain,
                "url": c.url
            }
            for c in companies
        ]
    }

@router.get("/monitoring/stats")
async def get_monitoring_stats(
    time_range: str = "7d",
    current_user: dict = Depends(get_current_user),
    db: Session = Depends(get_db)
):
    """
    Get monitoring statistics for the dashboard.
    Accessible by Super Admin (all data) and Company Admin (company specific).
    """
    require_admin(current_user, "company_admin")
    
    from db.models import Message, Task, User, Conversation, PTORequest, HRTicket, PTOStatus, TicketStatus
    from sqlalchemy import func, desc
    from datetime import datetime, timedelta, timezone
    import random
    
    # Determine scope
    is_super_admin = current_user["role"] == "super_admin"
    company_filter = None if is_super_admin else current_user.get("company")
    # Super-admin already skips auto-tenant-filter at the event-listener layer
    # (see db/tenant_context.py), so no explicit bypass is needed here.

    # Time range calculation
    now = datetime.now(timezone.utc)
    if time_range == "24h":
        start_date = now - timedelta(hours=24)
    elif time_range == "30d":
        start_date = now - timedelta(days=30)
    else: # default 7d
        start_date = now - timedelta(days=7)

    # 1. Request Count Over Time (Messages)
    query = db.query(
        func.date(Message.created_at).label('date'),
        func.count(Message.id).label('count')
    ).join(Conversation, Message.conversation_id == Conversation.id)
    
    if company_filter:
        query = query.filter(Conversation.company == company_filter)
        
    requests_over_time = query.filter(
        Message.created_at >= start_date,
        Message.role == 'user'
    ).group_by(
        func.date(Message.created_at)
    ).all()
    
    chart_data = [{"date": str(r.date), "requests": r.count} for r in requests_over_time]
    
    # 2. Agent Usage Breakdown (Super Admin Only)
    agent_data = []
    if is_super_admin:
        agent_query = db.query(
            Message.agent_type,
            func.count(Message.id).label('count')
        ).filter(
            Message.created_at >= start_date,
            Message.agent_type.isnot(None)
        ).group_by(Message.agent_type).all()
        
        agent_data = [
            {"name": r.agent_type or "General", "value": r.count} 
            for r in agent_query
        ]
    
    # 3. Active Users
    active_user_query = db.query(func.count(func.distinct(Conversation.email)))
    if company_filter:
        active_user_query = active_user_query.filter(Conversation.company == company_filter)
    active_users_count = active_user_query.filter(
        Conversation.updated_at >= (now - timedelta(days=30))
    ).scalar() or 0
    
    # 4. Recent Errors (Super Admin Only)
    recent_errors = []
    if is_super_admin:
        errors = db.query(Task).filter(
            Task.status == "failed",
            Task.created_at >= start_date
        ).order_by(desc(Task.created_at)).limit(10).all()
        
        recent_errors = [
            {
                "id": t.id,
                "type": t.task_type,
                "error": t.error,
                "time": t.created_at.isoformat()
            }
            for t in errors
        ]
        
    # 5. Company Activity Leaderboard (Super Admin Only)
    company_activity = []
    if is_super_admin:
        activity_query = db.query(
            Conversation.company,
            func.count(Message.id).label('count')
        ).join(Message, Conversation.id == Message.conversation_id).filter(
            Message.created_at >= start_date
        ).group_by(Conversation.company).order_by(desc('count')).limit(5).all()
        
        company_activity = [
            {"name": r.company, "requests": r.count}
            for r in activity_query
        ]
        
    # 6. Top Users (Company Admin Only)
    top_users = []
    if not is_super_admin and company_filter:
        top_user_query = db.query(
            Conversation.email,
            func.count(Message.id).label('count')
        ).join(Message, Conversation.id == Message.conversation_id).filter(
            Conversation.company == company_filter,
            Message.created_at >= start_date
        ).group_by(Conversation.email).order_by(desc('count')).limit(5).all()
        
        top_users = [
            {"email": r.email, "requests": r.count}
            for r in top_user_query
        ]
        
    # 7. Operational Stats (Company Admin Only)
    operational_stats = {}
    if not is_super_admin and company_filter:
        pending_pto = db.query(func.count(PTORequest.id)).filter(
            PTORequest.company == company_filter,
            PTORequest.status == PTOStatus.PENDING
        ).scalar() or 0
        
        open_tickets = db.query(func.count(HRTicket.id)).filter(
            HRTicket.company == company_filter,
            HRTicket.status.in_([TicketStatus.PENDING, TicketStatus.IN_PROGRESS])
        ).scalar() or 0
        
        operational_stats = {
            "pending_pto": pending_pto,
            "open_tickets": open_tickets
        }
    
    # 8. Response Time Distribution (Mocked)
    response_times = [
        {"range": "< 1s", "count": random.randint(20, 50)},
        {"range": "1-3s", "count": random.randint(40, 80)},
        {"range": "3-5s", "count": random.randint(10, 30)},
        {"range": "> 5s", "count": random.randint(5, 15)},
    ]
    
    total_requests = sum(item['requests'] for item in chart_data)
    error_count = len(recent_errors) if is_super_admin else 0
    error_rate = round((error_count / max(total_requests, 1)) * 100, 2)
    
    return {
        "overview": {
            "total_requests": total_requests,
            "active_users": active_users_count,
            "error_rate": error_rate,
            "avg_response_time": "1.2s"
        },
        "charts": {
            "requests_over_time": chart_data,
            "agent_usage": agent_data, # Empty for company admin
            "response_time_distribution": response_times,
            "company_activity": company_activity # Empty for company admin
        },
        "recent_errors": recent_errors, # Empty for company admin
        "top_users": top_users, # Empty for super admin
        "operational_stats": operational_stats # Empty for super admin
    }
# services/api/api/routers/users.py
from __future__ import annotations

from fastapi import APIRouter, Depends

from sa_common.db.users import User

from api.auth import get_current_user
from api.schemas import UserOut

router = APIRouter(tags=["users"])


@router.get("/me", response_model=UserOut)
def me(user: User = Depends(get_current_user)) -> UserOut:
    return UserOut(id=user.id, email=user.email, display_name=user.display_name)

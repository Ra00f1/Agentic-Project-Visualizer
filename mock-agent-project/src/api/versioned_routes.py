"""FastAPI router with versioned paths and Depends. Stresses L4's
endpoint discovery under nested include_router + prefix.
"""

from fastapi import APIRouter, Depends

v1_router = APIRouter(prefix="/v1")
v2_router = APIRouter(prefix="/v2")


def _get_user_id() -> str:
    return "stub-user"


@v1_router.get("/users/{user_id}/orders")
def list_orders_v1(user_id: str, uid: str = Depends(_get_user_id)):
    return {"version": 1, "user_id": user_id, "orders": []}


@v2_router.get("/users/{user_id}/orders")
def list_orders_v2(user_id: str, cursor: str | None = None, uid: str = Depends(_get_user_id)):
    return {"version": 2, "user_id": user_id, "orders": [], "cursor": cursor}


@v1_router.post("/orders")
def create_order_v1():
    return {"version": 1, "id": "stub-order"}


@v2_router.post("/orders")
def create_order_v2():
    return {"version": 2, "id": "stub-order"}

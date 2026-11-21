from fastapi import APIRouter

from app.api.v1 import health, twin

api_router = APIRouter()
api_router.include_router(health.router)
api_router.include_router(twin.router)

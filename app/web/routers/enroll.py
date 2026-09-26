"""Enroll Now: start a batch run and stream live progress."""
from fastapi import APIRouter, Form, Request
from fastapi.responses import JSONResponse

from ...services import enroll_service
from ..deps import check_csrf, require_user

router = APIRouter()


@router.post("/api/enroll-now")
async def enroll_now(request: Request, csrf_token: str = Form("")):
    user = require_user(request)
    if not check_csrf(csrf_token):
        return JSONResponse({"error": "Session expired - reload the page."}, status_code=400)
    return enroll_service.start_enroll_all(user["id"])


@router.post("/api/enroll-now/stop")
async def enroll_now_stop(request: Request, csrf_token: str = Form("")):
    user = require_user(request)
    if not check_csrf(csrf_token):
        return JSONResponse({"error": "Session expired - reload the page."}, status_code=400)
    return enroll_service.stop_enroll(user["id"])


@router.get("/api/enroll-now/status")
async def enroll_now_status(request: Request):
    user = require_user(request)
    return enroll_service.get_batch_status(user["id"])

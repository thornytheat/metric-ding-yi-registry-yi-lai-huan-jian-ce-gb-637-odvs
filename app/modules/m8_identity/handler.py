"""M8 入口：投递批次、查批次与现网、按名称+发布时间回溯、影响面与页面。"""
from __future__ import annotations

from fastapi import APIRouter, Body, Depends, Request

from ... import errors
from ...web import Ctx, get_ctx, render
from . import service

router = APIRouter(tags=["M8 身份与名称复用"])


@router.post("/api/identity/deliver")
def api_deliver(payload: dict | None = Body(default=None),
                ctx: Ctx = Depends(get_ctx)) -> dict:
    return service.deliver(ctx.store, ctx.settings, payload)


@router.get("/api/identity/rounds")
def api_rounds(ctx: Ctx = Depends(get_ctx)) -> dict:
    return service.list_rounds(ctx.store, ctx.settings)


@router.get("/api/identity/round")
def api_round(round_id: str, ctx: Ctx = Depends(get_ctx)) -> dict:
    return service.round_view(ctx.store, ctx.settings, round_id)


@router.get("/api/identity/live")
def api_live(ctx: Ctx = Depends(get_ctx)) -> dict:
    return service.live_view(ctx.store)


@router.get("/api/identity/impact")
def api_impact(identity: str | None = None,
               ctx: Ctx = Depends(get_ctx)) -> dict:
    return service.impact_view(ctx.store, identity)


@router.get("/api/identity/reports")
def api_reports(report_id: str | None = None,
                ctx: Ctx = Depends(get_ctx)) -> dict:
    return service.reports_view(ctx.store, ctx.settings, report_id=report_id)


@router.get("/api/identity/lookup")
def api_lookup(name: str, published_on: str | None = None,
               ctx: Ctx = Depends(get_ctx)) -> dict:
    return service.lookup(ctx.store, ctx.settings, name=name,
                          published_on=published_on)


@router.get("/identity")
def page_identity(request: Request, identity: str | None = None,
                  ctx: Ctx = Depends(get_ctx)):
    payload = service.snapshot_view(ctx.store, ctx.settings)
    impact = None
    if payload.get("live"):
        try:
            impact = service.impact_view(ctx.store, identity)
        except errors.MetricregError:
            impact = None
    return render(request, "identity.html", page="identity",
                  payload=payload, impact=impact, page_title="身份与名称复用")

"""Market landing-page API endpoints."""

from __future__ import annotations

from typing import Optional

from fastapi import APIRouter, Depends, HTTPException, Query

from src.config.settings import Settings, get_settings
from src.data import screener as screener_data
from src.web.services import market_service

router = APIRouter(
    prefix="/api/v1/market",
    tags=["market"],
)


@router.get("/overview")
def overview(
    size: int = Query(
        default=market_service.DEFAULT_PANEL_ROWS,
        ge=1,
        le=market_service.MAX_PANEL_ROWS,
        description="Rows per panel",
    ),
    force: bool = Query(default=False, description="Bypass the in-memory cache"),
    _: Settings = Depends(get_settings),
) -> dict:
    """Every landing-page panel (whole market, gainers, volume, losers, small caps)."""
    return market_service.overview(size=size, force=force)


@router.get("/panel/{key}")
def panel(
    key: str,
    size: int = Query(
        default=market_service.DEFAULT_PANEL_ROWS, ge=1, le=market_service.MAX_PANEL_ROWS
    ),
    offset: int = Query(default=0, ge=0, description="Row offset, for paging long lists"),
    _: Settings = Depends(get_settings),
) -> dict:
    """A single panel — used to page through a long list (e.g. the whole market)."""
    try:
        return market_service.panel(key, size=size, offset=offset)
    except screener_data.ScreenerError as exc:
        raise HTTPException(status_code=404, detail=str(exc)) from exc


@router.get("/presets")
def presets(_: Settings = Depends(get_settings)) -> dict:
    """Yahoo's predefined screener list, for the screener panel's selector."""
    return market_service.presets()


@router.get("/screen")
def screen(
    preset: Optional[str] = Query(default=None, description="Preset key, e.g. day_gainers"),
    size: int = Query(default=market_service.DEFAULT_PANEL_ROWS, ge=1),
    market_cap_min: Optional[int] = Query(default=None, ge=0, description="Market cap floor (USD)"),
    market_cap_max: Optional[int] = Query(default=None, ge=0, description="Market cap ceiling (USD)"),
    min_price: float = Query(default=1.0, ge=0, description="Price floor, filters penny stocks"),
    min_volume: int = Query(default=0, ge=0, description="Day-volume floor"),
    _: Settings = Depends(get_settings),
) -> dict:
    """Run one preset screener with custom filters."""
    return market_service.screen(
        preset=preset,
        size=size,
        market_cap_min=market_cap_min,
        market_cap_max=market_cap_max,
        min_price=min_price,
        min_volume=min_volume,
    )


@router.post("/refresh")
def refresh(_: Settings = Depends(get_settings)) -> dict:
    """Drop the cached overview and refetch every panel."""
    return market_service.refresh()

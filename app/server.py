"""
FastAPI web server for the Eurowings Blind Booking finder.

Endpoints:
  GET  /                    → index.html
  GET  /airports            → hardcoded airport list
  POST /search              → start a search job, returns {search_id}
  GET  /search/{id}/stream  → SSE stream of progress + results

Windows note: uvicorn forces SelectorEventLoop on Windows, but Playwright
needs ProactorEventLoop to spawn the Chromium subprocess.  The fix is to
run all Playwright work inside a dedicated thread that owns a fresh
ProactorEventLoop (or a plain asyncio loop on Linux).  Events are posted
back to the main uvicorn queue via loop.call_soon_threadsafe().
"""

from __future__ import annotations

import asyncio
import concurrent.futures
import json
import sys
import uuid
from datetime import datetime
from pathlib import Path
from typing import AsyncIterator, Callable

LOGS_DIR = Path(__file__).parent.parent / "logs"
LOGS_DIR.mkdir(exist_ok=True)
LOG_FILE = LOGS_DIR / "blind_booking.log"

from fastapi import FastAPI, BackgroundTasks, HTTPException
from fastapi.responses import HTMLResponse, StreamingResponse
from fastapi.templating import Jinja2Templates
from starlette.requests import Request
from pydantic import BaseModel

from playwright.async_api import async_playwright

from app import browser as br
from app.algorithm import classify_destinations

# ---------------------------------------------------------------------------
# App setup
# ---------------------------------------------------------------------------

app = FastAPI(title="Blind Booking Finder")
templates = Jinja2Templates(directory=Path(__file__).parent / "templates")

# search_id → asyncio.Queue (populated from the Playwright thread, consumed by SSE)
_queues: dict[str, asyncio.Queue] = {}

# Thread pool used to host the per-search Playwright event loop
_executor = concurrent.futures.ThreadPoolExecutor(max_workers=16)

# ---------------------------------------------------------------------------
# Airport list (hardcoded from page exploration)
# ---------------------------------------------------------------------------

AIRPORTS = [
    {"code": "BER", "name": "Berlin (BER)"},
    {"code": "DUS", "name": "Düsseldorf (DUS)"},
    {"code": "GRZ", "name": "Graz (GRZ)"},
    {"code": "HAM", "name": "Hamburg (HAM)"},
    {"code": "HAJ", "name": "Hannover (HAJ)"},
    {"code": "CGN", "name": "Köln-Bonn (CGN)"},
    {"code": "PMI", "name": "Palma de Mallorca (PMI)"},
    {"code": "PRG", "name": "Prag (PRG)"},
    {"code": "SZG", "name": "Salzburg (SZG)"},
    {"code": "ARN", "name": "Stockholm (ARN)"},
    {"code": "STR", "name": "Stuttgart (STR)"},
]

# ---------------------------------------------------------------------------
# Request model
# ---------------------------------------------------------------------------

class SearchParams(BaseModel):
    airport: str
    outbound: str = ""      # DD.MM.YYYY or ""
    return_date: str = ""   # DD.MM.YYYY or ""
    adults: int = 1
    children: int = 0
    infants: int = 0
    min_nights: int = 2     # ≥ 2 (Eurowings minimum)
    max_nights: int = 7


# ---------------------------------------------------------------------------
# SSE helpers
# ---------------------------------------------------------------------------

def _sse(event: str, data: dict) -> str:
    return f"event: {event}\ndata: {json.dumps(data, ensure_ascii=False)}\n\n"


# ---------------------------------------------------------------------------
# Playwright thread: runs all browser work in its own ProactorEventLoop
# ---------------------------------------------------------------------------

def _make_loop() -> asyncio.AbstractEventLoop:
    """Return a fresh event loop that can spawn subprocesses on Windows."""
    if sys.platform == "win32":
        return asyncio.ProactorEventLoop()
    loop = asyncio.new_event_loop()
    return loop


async def _search_theme(
    browser,
    theme: dict,
    params: SearchParams,
    emit: Callable[[str, dict], None],
) -> None:
    """Run the full binary search for one theme; emit() posts SSE events."""
    theme_name = theme["name"]
    ctx = await br.new_context(browser)
    page = await br.new_stealth_page(ctx)

    try:
        emit("theme_start", {"theme": theme_name})

        cities = await br.navigate_to_form(
            page,
            airport=params.airport,
            theme_name=theme_name,
            outbound=params.outbound,
            return_date=params.return_date,
            adults=params.adults,
            children=params.children,
            infants=params.infants,
            min_nights=params.min_nights,
            max_nights=params.max_nights,
        )

        if not cities:
            emit("theme_done", {
                "theme": theme_name,
                "error": "Could not load city list",
                "available_cities": [],
                "cheapest_price": None,
            })
            return

        emit("cities_loaded", {"theme": theme_name, "cities": cities})

        async def test_fn(selected: list[str]) -> str | None:
            return await br.test_selection(page, selected)

        async def on_progress(description: str, selection: list[str], has_price: bool) -> None:
            emit("progress", {
                "theme": theme_name,
                "step": description,
                "has_price": has_price,
            })

        async def on_city_result(city: str, state: str) -> None:
            emit("city_result", {"theme": theme_name, "city": city, "state": state})

        classified = await classify_destinations(cities, test_fn, on_progress, on_city_result)

        certain   = [c for c, s in classified.items() if s == "available"]
        uncertain = [c for c, s in classified.items() if s == "uncertain"]
        emit("theme_done", {
            "theme": theme_name,
            "available_cities": certain,
            "uncertain_cities": uncertain,
        })

    except Exception as e:
        emit("theme_done", {
            "theme": theme_name,
            "error": str(e),
            "available_cities": [],
            "cheapest_price": None,
        })
    finally:
        await ctx.close()


async def _do_search(params: SearchParams, emit: Callable[[str, dict], None]) -> None:
    """All Playwright work; runs inside the dedicated ProactorEventLoop thread."""
    try:
        async with async_playwright() as pw:
            browser = await br.create_browser(pw)

            # Discover themes
            ctx0 = await br.new_context(browser)
            page0 = await br.new_stealth_page(ctx0)
            themes = await br.get_themes(page0, params.airport)
            await ctx0.close()
            if not themes:
                emit("error", {"message": "No themes found for this airport"})
                return

            emit("themes_discovered", {
                "themes": [t["name"] for t in themes],
                "count": len(themes),
            })

            # All themes in parallel (each gets its own BrowserContext)
            await asyncio.gather(*[
                _search_theme(browser, theme, params, emit)
                for theme in themes
            ])

            await browser.close()

    except Exception as e:
        emit("error", {"message": str(e)})


async def _run_search(search_id: str, params: SearchParams) -> None:
    """
    Spawns a thread with a Playwright-compatible event loop.
    Events are marshalled back to the uvicorn queue via call_soon_threadsafe.
    """
    q = _queues[search_id]
    main_loop = asyncio.get_running_loop()

    sid = search_id[:8]
    with LOG_FILE.open("a", encoding="utf-8") as _lf:
        _lf.write(
            f"\n{'─' * 60}\n"
            f"[{datetime.now().isoformat()}] search={sid} params={params.model_dump_json()}\n"
        )

    def _log(event: str, data: dict) -> None:
        try:
            with LOG_FILE.open("a", encoding="utf-8") as f:
                ts = datetime.now().strftime("%H:%M:%S")
                f.write(f"{ts} {sid} [{event}] {json.dumps(data, ensure_ascii=False)}\n")
        except Exception:
            pass

    # Thread-safe emitter: posts events from the Playwright thread → uvicorn queue
    def emit(event: str, data: dict) -> None:
        _log(event, data)
        main_loop.call_soon_threadsafe(q.put_nowait, _sse(event, data))

    def thread_fn() -> None:
        loop = _make_loop()
        asyncio.set_event_loop(loop)
        try:
            loop.run_until_complete(_do_search(params, emit))
        finally:
            loop.close()
            # Sentinel: tells the SSE generator the stream is finished
            main_loop.call_soon_threadsafe(q.put_nowait, None)

    await main_loop.run_in_executor(_executor, thread_fn)


# ---------------------------------------------------------------------------
# Routes
# ---------------------------------------------------------------------------

@app.get("/", response_class=HTMLResponse)
async def index(request: Request):
    return templates.TemplateResponse(request, "index.html", {"airports": AIRPORTS})


@app.get("/airports")
async def airports():
    return AIRPORTS


@app.post("/search")
async def start_search(params: SearchParams, background_tasks: BackgroundTasks):
    search_id = str(uuid.uuid4())
    _queues[search_id] = asyncio.Queue()
    background_tasks.add_task(_run_search, search_id, params)
    return {"search_id": search_id}


@app.get("/search/{search_id}/stream")
async def stream(search_id: str):
    if search_id not in _queues:
        raise HTTPException(status_code=404, detail="Search not found")

    q = _queues[search_id]

    async def event_generator() -> AsyncIterator[str]:
        yield ": keep-alive\n\n"
        while True:
            item = await asyncio.wait_for(q.get(), timeout=300)
            if item is None:
                _queues.pop(search_id, None)
                return
            yield item

    return StreamingResponse(event_generator(), media_type="text/event-stream")

"""
Async Playwright wrapper for the Eurowings Blind Booking site.

Page flow:
  /travel-theme  → select airport pill + Reisethema radio → "Weiter"
  /compose-trip  (form)   → city checkboxes + dates + passengers → "Weiter"
  /compose-trip  (result) → price shown → "Zurück" (for next test) or done
"""

from __future__ import annotations

import asyncio
import json
import os
import time
from pathlib import Path
from typing import Optional

from playwright.async_api import (
    async_playwright,
    Browser,
    BrowserContext,
    Page,
    Playwright,
)
from playwright_stealth import Stealth

# ---------------------------------------------------------------------------
# Constants
# ---------------------------------------------------------------------------

SITE_URL         = "https://blindbooking.eurowings.com/compose-trip"
TRAVEL_THEME_URL = "https://blindbooking.eurowings.com/travel-theme"
HEADLESS = os.environ.get("HEADLESS", "true").lower() == "true"
SLOW_MO  = int(os.environ.get("SLOW_MO", "0"))

# Cookies saved from a successful session are reloaded in future runs so the
# browser appears as a returning visitor rather than a fresh automated session.
COOKIE_FILE = Path(__file__).parent.parent / "logs" / "browser_cookies.json"

# Per-page state: id(page) → {"registry": {name_lower: value}, "checked": set()}
_page_state: dict[int, dict] = {}


# ---------------------------------------------------------------------------
# Browser / context lifecycle
# ---------------------------------------------------------------------------

async def create_browser(pw: Playwright) -> Browser:
    return await pw.chromium.launch(
        headless=HEADLESS,
        slow_mo=SLOW_MO,
        args=["--disable-blink-features=AutomationControlled"],
    )


async def new_context(browser: Browser) -> BrowserContext:
    ctx = await browser.new_context(
        viewport={"width": 1400, "height": 900},
        locale="de-DE",
        user_agent=(
            "Mozilla/5.0 (Windows NT 10.0; Win64; x64) "
            "AppleWebKit/537.36 (KHTML, like Gecko) "
            "Chrome/124.0.0.0 Safari/537.36"
        ),
    )
    await _load_cookies(ctx)
    return ctx


async def _load_cookies(ctx: BrowserContext) -> None:
    if not COOKIE_FILE.exists():
        return
    try:
        cookies = json.loads(COOKIE_FILE.read_text(encoding="utf-8"))
        now = time.time()
        valid = [c for c in cookies
                 if c.get("expires", -1) < 0 or c.get("expires", now + 1) > now]
        if valid:
            await ctx.add_cookies(valid)
    except Exception:
        pass


async def _save_cookies(ctx: BrowserContext) -> None:
    try:
        cookies = await ctx.cookies()
        COOKIE_FILE.parent.mkdir(exist_ok=True)
        COOKIE_FILE.write_text(json.dumps(cookies, indent=2), encoding="utf-8")
    except Exception:
        pass


async def new_stealth_page(ctx: BrowserContext) -> Page:
    page = await ctx.new_page()
    await Stealth().apply_stealth_async(page)
    return page


# ---------------------------------------------------------------------------
# Cookie consent
# ---------------------------------------------------------------------------

async def accept_cookies(page: Page) -> None:
    for sel in [
        "button:has-text('Alle akzeptieren')",
        "button:has-text('Akzeptieren')",
        "button:has-text('Accept all')",
        "button:has-text('Zustimmen')",
    ]:
        try:
            btn = page.locator(sel).first
            if await btn.is_visible(timeout=1500):
                await btn.click()
                await page.wait_for_timeout(800)
                return
        except Exception:
            pass


# ---------------------------------------------------------------------------
# Page-state detection
# ---------------------------------------------------------------------------

async def current_state(page: Page) -> str:
    url = page.url
    if "travel-theme" in url:
        return "travel-theme"
    if "compose-trip" in url:
        try:
            if await page.locator("button:has-text('Weiter zur Buchung')").is_visible(timeout=400):
                return "compose-trip:result"
        except Exception:
            pass
        return "compose-trip:form"
    if "fare" in url:
        return "fare"
    return url


# ---------------------------------------------------------------------------
# /travel-theme interactions
# ---------------------------------------------------------------------------

# IATA code → unique city name shown on the airport chip
_AIRPORT_CITY: dict[str, str] = {
    "BER": "Berlin",
    "DUS": "Düsseldorf",
    "GRZ": "Graz",
    "HAM": "Hamburg",
    "HAJ": "Hannover",
    "CGN": "Köln-Bonn",
    "PMI": "Palma de Mallorca",
    "PRG": "Prag",
    "SZG": "Salzburg",
    "ARN": "Stockholm",
    "STR": "Stuttgart",
}


async def select_airport(page: Page, airport: str) -> None:
    """Click the airport chip whose label contains the city name for this IATA code."""
    # Resolve IATA code → city name; fall back to the raw code
    city = _AIRPORT_CITY.get(airport.upper(), airport)

    # Airport chips have class 'chip-span origins'; city names are unique on the page
    for sel in [
        f".chip-span:has-text('{city}')",
        f".chip:has-text('{city}')",
        f"button:has-text('{city}')",
    ]:
        try:
            el = page.locator(sel).first
            if await el.is_visible(timeout=1000):
                await el.click()
                await page.wait_for_timeout(400)
                return
        except Exception:
            pass

    # JS fallback — chips only, match by city name
    await page.evaluate(f"""
        () => {{
            const chips = [...document.querySelectorAll('.chip-span, .chip')];
            const m = chips.find(e => e.innerText && e.innerText.includes('{city}')
                                      && e.offsetParent !== null);
            if (m) m.click();
        }}
    """)
    await page.wait_for_timeout(300)


async def get_themes(page: Page, airport: str) -> list[dict]:
    """
    Return list of available themes for the given airport.
    Each entry: {"name": str, "price_preview": str}
    """
    await page.goto(TRAVEL_THEME_URL, wait_until="domcontentloaded", timeout=60_000)
    await accept_cookies(page)

    # Wait for airport chips to be interactive before proceeding
    try:
        await page.locator(".chip-span, .chip").first.wait_for(state="visible", timeout=8000)
    except Exception:
        pass
    await page.wait_for_timeout(500)

    count_before = await page.locator("input[name='pool']").count()
    await select_airport(page, airport)

    # Wait until the theme list refreshes
    try:
        await page.wait_for_function(
            f"document.querySelectorAll(\"input[name='pool']\").length !== {count_before}",
            timeout=6000,
        )
    except Exception:
        pass

    # If count didn't change, the chip click may have missed — retry once
    count_after = await page.locator("input[name='pool']").count()
    if count_after == count_before:
        await select_airport(page, airport)
        try:
            await page.wait_for_function(
                f"document.querySelectorAll(\"input[name='pool']\").length !== {count_before}",
                timeout=4000,
            )
        except Exception:
            pass

    # Extra settling time for labels to render
    await page.wait_for_timeout(600)

    themes = []
    radios = await page.locator("input[name='pool']").all()
    for r in radios:
        try:
            el_id = await r.get_attribute("id")
            name = ""
            price = ""
            if el_id:
                lbl = page.locator(f"label[for='{el_id}']")
                if await lbl.count() > 0:
                    name = (await lbl.first.inner_text(timeout=300)).strip()
            # Price hint is usually in a sibling span/div after the label
            try:
                parent = page.locator(f"label[for='{el_id}']").locator("..")
                price_el = parent.locator("*:has-text('€')").first
                price = (await price_el.inner_text(timeout=300)).strip()
            except Exception:
                pass
            if name:
                themes.append({"name": name, "price_preview": price})
        except Exception:
            pass
    return themes


async def select_theme(page: Page, theme_name: str) -> None:
    """Select the radio whose label matches theme_name (first radio if empty)."""
    radios = await page.locator("input[name='pool']").all()
    target = None
    if theme_name:
        for r in radios:
            try:
                el_id = await r.get_attribute("id")
                if el_id:
                    lbl = page.locator(f"label[for='{el_id}']")
                    txt = (await lbl.first.inner_text(timeout=300)).lower()
                    if theme_name.lower() in txt:
                        target = r
                        break
            except Exception:
                pass
    if target is None and radios:
        target = radios[0]
    if target and not await target.is_checked():
        await target.click()
    await page.wait_for_timeout(400)


# ---------------------------------------------------------------------------
# /compose-trip:form interactions
# ---------------------------------------------------------------------------

async def get_cities(page: Page) -> list[dict]:
    """
    Return all destination checkboxes.
    Each entry: {"name": str, "value": str, "checked": bool}
    """
    result = []
    checkboxes = await page.locator("input[name='poolDestination']").all()
    for cb in checkboxes:
        try:
            cb_id  = await cb.get_attribute("id") or ""
            value  = await cb.get_attribute("value") or ""
            label  = ""
            if cb_id:
                lbl = page.locator(f"label[for='{cb_id}']")
                if await lbl.count() > 0:
                    label = (await lbl.first.inner_text(timeout=300)).strip()
            if not label:
                parent = cb.locator("xpath=ancestor::label[1]")
                if await parent.count() > 0:
                    label = (await parent.first.inner_text(timeout=300)).strip()
            result.append({
                "name":    label or value,
                "value":   value,
                "checked": await cb.is_checked(),
            })
        except Exception:
            pass
    return result


async def set_city_selection(page: Page, selected_names: list[str]) -> None:
    """
    Set checkboxes so that exactly the cities in selected_names are checked.
    Uses cached name→value registry and tracks current state to only operate
    on the delta. Playwright's check()/uncheck() dispatch the full event
    sequence React needs, including the click that transitions back to form mode.
    """
    state = _page_state.get(id(page))
    if state is None:
        return

    registry = state["registry"]          # name_lower → checkbox value attr
    current  = state["checked"]           # set of name_lower currently checked

    wanted     = {n.lower() for n in selected_names}
    to_uncheck = current - wanted
    to_check   = wanted - current

    # Check new cities first so count never drops below page minimum, then uncheck.
    # Two separate evaluate() calls so React has time to process each batch.
    check_vals   = [registry[n] for n in to_check   if n in registry]
    uncheck_vals = [registry[n] for n in to_uncheck if n in registry]

    if check_vals:
        await page.evaluate(
            """(vals) => {
                for (const v of vals) {
                    const cb = document.querySelector(
                        `input[name='poolDestination'][value='${v}']`
                    );
                    if (cb && !cb.checked) cb.click();
                }
            }""",
            check_vals,
        )
        await page.wait_for_timeout(150)

    if uncheck_vals:
        await page.evaluate(
            """(vals) => {
                for (const v of vals) {
                    const cb = document.querySelector(
                        `input[name='poolDestination'][value='${v}']`
                    );
                    if (cb && cb.checked) cb.click();
                }
            }""",
            uncheck_vals,
        )

    state["checked"] = wanted
    await page.wait_for_timeout(200)


_DE_MONTHS = [
    "Januar","Februar","März","April","Mai","Juni",
    "Juli","August","September","Oktober","November","Dezember",
]
_EN_MONTHS = [
    "January","February","March","April","May","June",
    "July","August","September","October","November","December",
]

async def _pick_calendar_date(page: Page, field_id: str, day: int, month: int, year: int) -> None:
    """Click through the react-datepicker calendar to select a specific date."""
    inp = page.locator(f"#{field_id}").first
    await inp.click()
    await page.wait_for_timeout(600)

    cal = page.locator(".react-datepicker").first

    for _ in range(24):   # navigate at most 24 months
        header_text = (await cal.locator(".react-datepicker__current-month").first.inner_text(timeout=2000)).strip()
        # Parse current month/year from header ("Mai 2026" or "May 2026")
        cur_month, cur_year = 0, 0
        for i, name in enumerate(_DE_MONTHS + _EN_MONTHS):
            if name in header_text:
                cur_month = (i % 12) + 1
                break
        try:
            cur_year = int(header_text.split()[-1])
        except Exception:
            pass

        if cur_month == month and cur_year == year:
            break
        # Decide direction
        cur_total  = cur_year * 12 + cur_month
        tgt_total  = year * 12 + month
        nav_sel    = ".react-datepicker__navigation--next" if tgt_total > cur_total else ".react-datepicker__navigation--previous"
        await cal.locator(nav_sel).first.click()
        await page.wait_for_timeout(300)

    # Click the correct day (react-datepicker uses zero-padded 3-digit class like --015)
    day_cls = f".react-datepicker__day--{day:03d}:not(.react-datepicker__day--outside-month)"
    await cal.locator(day_cls).first.click()
    await page.wait_for_timeout(400)


async def set_dates(page: Page, outbound: str, return_date: str) -> None:
    """
    Set dates by clicking through the react-datepicker calendar UI.
    Programmatic value setting is overridden by React; clicking calendar
    days is the only reliable approach.
    Format: DD.MM.YYYY
    """
    async def pick(field_id: str, val: str) -> None:
        if not val:
            return
        try:
            day, month, year = int(val[:2]), int(val[3:5]), int(val[6:])
            await _pick_calendar_date(page, field_id, day, month, year)
        except Exception:
            pass

    # Set return date first so outbound is never temporarily after it
    await pick("latestRet", return_date)
    await pick("earliestOut", outbound)


async def set_nights(page: Page, min_nights: int, max_nights: int) -> None:
    """
    Set min/max nights via the select elements inside the plusMinus widget.
    Field names: minStay / maxStay  (same naming convention as numAdt etc.)
    """
    for name, val in [("minStay", min_nights), ("maxStay", max_nights)]:
        try:
            sel = page.locator(f"select[name='{name}']").first
            await sel.select_option(value=str(val))
            await page.wait_for_timeout(150)
        except Exception:
            pass
        # JS fallback in case the option is technically disabled by date range
        try:
            await page.evaluate(f"""() => {{
                const el = document.querySelector("select[name='{name}']");
                if (!el) return;
                el.value = '{val}';
                el.dispatchEvent(new Event('change', {{bubbles: true}}));
            }}""")
        except Exception:
            pass


async def set_passengers(page: Page, adults: int, children: int, infants: int) -> None:
    """Set passenger counts via select elements, with JS fallback."""
    for name, val in [("numAdt", adults), ("numChd", children), ("numInf", infants)]:
        try:
            sel = page.locator(f"select[name='{name}']").first
            await sel.select_option(value=str(val))
            await page.wait_for_timeout(150)
        except Exception:
            pass
        # JS fallback
        try:
            await page.evaluate(f"""() => {{
                const el = document.querySelector("select[name='{name}']");
                if (el) {{
                    el.value = '{val}';
                    el.dispatchEvent(new Event('change', {{bubbles: true}}));
                }}
            }}""")
        except Exception:
            pass


# ---------------------------------------------------------------------------
# Navigation helpers
# ---------------------------------------------------------------------------

async def click_weiter(page: Page) -> bool:
    """Click the forward Weiter/Weiter zur Buchung button."""
    for sel in [
        "button:has-text('Weiter zur Buchung')",
        "button:has-text('Weiter')",
        "a:has-text('Weiter')",
        ".bttn_forw",
        ".js-submit.bttn_forw",
    ]:
        try:
            btn = page.locator(sel).first
            await btn.wait_for(state="visible", timeout=4000)
            await btn.scroll_into_view_if_needed()
            try:
                await btn.click(timeout=3000)
            except Exception:
                await btn.evaluate("el => el.click()")
            return True
        except Exception:
            pass
    return False


async def click_zurueck(page: Page) -> bool:
    """Click the Zurück (back) button."""
    for sel in [
        "button:has-text('Zurück')",
        "a:has-text('Zurück')",
        ".bttn_back",
    ]:
        try:
            btn = page.locator(sel).first
            if await btn.is_visible(timeout=2000):
                await btn.click()
                await page.wait_for_timeout(1200)
                return True
        except Exception:
            pass
    return False


async def extract_price(page: Page, timeout_s: float = 8.0) -> Optional[str]:
    """Wait up to timeout_s seconds for a price (€) to appear; return it or None."""
    selectors = [
        "[class*='gesamtpreis' i]",
        "[class*='total' i]",
        "[class*='price' i]",
        "[class*='amount' i]",
        "strong:has-text('€')",
        "span:has-text('€')",
        "p:has-text('€')",
    ]
    deadline = time.monotonic() + timeout_s
    while time.monotonic() < deadline:
        for sel in selectors:
            try:
                els = await page.locator(sel).all()
                for el in els:
                    text = (await el.inner_text(timeout=300)).strip()
                    if "€" in text and any(c.isdigit() for c in text) and len(text) < 80:
                        return text
            except Exception:
                pass
        await asyncio.sleep(0.4)
    return None


# ---------------------------------------------------------------------------
# High-level: navigate to compose-trip:form for a given theme
# ---------------------------------------------------------------------------

async def navigate_to_form(
    page: Page,
    airport: str,
    theme_name: str,
    outbound: str,
    return_date: str,
    adults: int,
    children: int,
    infants: int,
    min_nights: int = 2,
    max_nights: int = 7,
) -> list[str]:
    """
    Load the site, pick airport + theme, arrive at compose-trip:form.
    Set dates and passengers. Return list of city names.
    """
    await page.goto(TRAVEL_THEME_URL, wait_until="domcontentloaded", timeout=60_000)
    await accept_cookies(page)

    # Wait for airport chips to be interactive
    try:
        await page.locator(".chip-span, .chip").first.wait_for(state="visible", timeout=8000)
    except Exception:
        pass
    await page.wait_for_timeout(500)

    # Step through the wizard
    for _ in range(6):
        state = await current_state(page)
        if state == "travel-theme":
            count_before = await page.locator("input[name='pool']").count()
            await select_airport(page, airport)
            try:
                await page.wait_for_function(
                    f"document.querySelectorAll(\"input[name='pool']\").length !== {count_before}",
                    timeout=6000,
                )
            except Exception:
                pass
            # Retry if count didn't change
            count_after = await page.locator("input[name='pool']").count()
            if count_after == count_before:
                await select_airport(page, airport)
                try:
                    await page.wait_for_function(
                        f"document.querySelectorAll(\"input[name='pool']\").length !== {count_before}",
                        timeout=4000,
                    )
                except Exception:
                    pass
            await page.wait_for_timeout(400)
            await select_theme(page, theme_name)
            await click_weiter(page)
            await page.wait_for_timeout(1500)

        elif state == "compose-trip:form":
            await _save_cookies(page.context)
            await set_dates(page, outbound, return_date)
            await set_nights(page, min_nights, max_nights)
            await set_passengers(page, adults, children, infants)
            cities = await get_cities(page)
            _page_state[id(page)] = {
                "registry": {c["name"].lower(): c["value"] for c in cities},
                "checked":  {c["name"].lower() for c in cities if c["checked"]},
            }
            return [c["name"] for c in cities]

        else:
            await page.wait_for_timeout(800)

    return []


# ---------------------------------------------------------------------------
# High-level: binary-search test step
# ---------------------------------------------------------------------------

async def _wait_for_result(page: Page, timeout_s: float = 12.0) -> Optional[str]:
    """
    After clicking Weiter, poll until either a price or the no-flight message
    appears.  Returns the price string or None.  Short-circuits as soon as
    the no-flight indicator is visible so we don't burn the full timeout.
    """
    price_selectors = [
        "[class*='gesamtpreis' i]",
        "[class*='total' i]",
        "[class*='price' i]",
        "[class*='amount' i]",
        "strong:has-text('€')",
        "span:has-text('€')",
        "p:has-text('€')",
    ]
    no_flight_texts = [
        "Bitte erhöhe",
        "Flexibilität",
        "keine Flüge",
        "nicht verfügbar",
        "leider keine",
    ]
    deadline = time.monotonic() + timeout_s
    while time.monotonic() < deadline:
        # Check for price
        for sel in price_selectors:
            try:
                els = await page.locator(sel).all()
                for el in els:
                    text = (await el.inner_text(timeout=200)).strip()
                    if "€" in text and any(c.isdigit() for c in text) and len(text) < 80:
                        return text
            except Exception:
                pass
        # Short-circuit on no-flight message
        for msg in no_flight_texts:
            try:
                if await page.locator(f"text={msg}").first.is_visible(timeout=200):
                    return None
            except Exception:
                pass
        await asyncio.sleep(0.4)
    return None


async def test_selection(page: Page, selected_names: list[str]) -> Optional[str]:
    """
    Change the city selection, wait for the Weiter button to (re)appear,
    click it, and wait for a price or no-flight message.

    Works both on the initial form and on a result page from a previous call —
    changing checkboxes on the result page makes Weiter reappear automatically,
    so no Zurück navigation is needed.

    Returns price string if a price is shown, else None.
    """
    await set_city_selection(page, selected_names)
    await page.wait_for_timeout(400)
    try:
        await page.get_by_role("button", name="Weiter", exact=True).first.wait_for(
            state="visible", timeout=5000
        )
    except Exception:
        pass

    try:
        await page.get_by_role("button", name="Weiter", exact=True).first.click(timeout=3000)
    except Exception:
        await click_weiter(page)

    await page.wait_for_timeout(600)
    return await _wait_for_result(page, timeout_s=12.0)

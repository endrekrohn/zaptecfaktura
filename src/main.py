import io
import logging
import secrets
import zipfile
from contextlib import asynccontextmanager
from datetime import date
from urllib.parse import urlencode

import polars as pl
from fastapi import BackgroundTasks, Depends, FastAPI, Form, Query, Request, Response
from fastapi.responses import HTMLResponse, RedirectResponse, StreamingResponse
from fastapi.staticfiles import StaticFiles
from fastapi.templating import Jinja2Templates

import database
from billing import (
    InstallationUsage,
    ValidationError,
    month_range,
    nok_to_ore,
    parse_nok_per_kwh,
    pdf_filename,
    previous_month,
    total_kwh,
    validate_period,
)
from config import BASE_DIR, settings
from drafts import STATUSES, plan_drafts, run_draft_job
from fiken_api import FikenAPI, FikenError
from invoice import format_month_to_norwegian, format_norwegian_accounting, generate_invoice_pdf
from zaptec_api import ZaptecAPI, ZaptecAuthError, ZaptecError

log = logging.getLogger(__name__)

SESSION_COOKIE = "session_id"
PUBLIC_PATHS = ("/static", "/login", "/health")

zaptec_api = ZaptecAPI(settings.zaptec_api_url)
fiken_api = (
    FikenAPI(settings.fiken_api_url, settings.fiken_api_token, settings.fiken_company_slug)
    if settings.fiken_enabled
    else None
)


@asynccontextmanager
async def lifespan(app: FastAPI):
    database.init_db(settings.database_path)
    database.delete_expired_sessions()
    database.fail_unfinished_drafts()
    if settings.fiken_enabled and not settings.allowed_users:
        log.warning(
            "Fiken is configured but ALLOWED_USERS is empty: all logins are refused."
        )
    yield


app = FastAPI(lifespan=lifespan)
app.mount("/static", StaticFiles(directory=BASE_DIR / "static"), name="static")
templates = Jinja2Templates(directory=BASE_DIR / "templates")
templates.env.filters["nok"] = lambda ore: format_norwegian_accounting(ore / 100)
templates.env.filters["month_name"] = lambda m: (format_month_to_norwegian(m) or "").capitalize()


class PageError(Exception):
    def __init__(self, message: str, status_code: int = 400):
        self.message = message
        self.status_code = status_code


@app.exception_handler(PageError)
async def page_error_handler(request: Request, exc: PageError):
    session = getattr(request.state, "session", None)
    return templates.TemplateResponse(
        request,
        "error.html",
        {"error": exc.message, "user": session["user"] if session else None},
        status_code=exc.status_code,
    )


@app.exception_handler(ValidationError)
async def validation_error_handler(request: Request, exc: ValidationError):
    return await page_error_handler(request, PageError(str(exc)))


@app.exception_handler(ZaptecAuthError)
async def zaptec_auth_handler(request: Request, exc: ZaptecAuthError):
    # The Zaptec token has expired or been revoked: start a new login.
    session_id = request.cookies.get(SESSION_COOKIE)
    if session_id:
        database.delete_session(session_id)
    return _redirect_to_login()


@app.exception_handler(ZaptecError)
async def zaptec_error_handler(request: Request, exc: ZaptecError):
    log.exception("Zaptec request failed")
    return await page_error_handler(
        request, PageError("Kunne ikke hente data fra Zaptec. Prøv igjen senere.", 502)
    )


def _redirect_to_login() -> RedirectResponse:
    response = RedirectResponse(url="/login", status_code=303)
    response.delete_cookie(SESSION_COOKIE)
    return response


@app.middleware("http")
async def auth_middleware(request: Request, call_next):
    path = request.url.path
    session = database.get_session(request.cookies.get(SESSION_COOKIE))
    request.state.session = session

    if path.startswith(PUBLIC_PATHS):
        if path.startswith("/login") and session:
            return RedirectResponse(url="/", status_code=303)
        return await call_next(request)

    if not session:
        return _redirect_to_login()
    return await call_next(request)


def get_token(request: Request) -> str:
    return request.state.session["access_token"]


def overview_url(year: int, month: int, nok_per_kwh: str | None = None) -> str:
    params = {"year": year, "month": month}
    if nok_per_kwh:
        params["nok_per_kwh"] = nok_per_kwh
    return f"/?{urlencode(params)}"


# --- auth -----------------------------------------------------------------


@app.get("/health")
async def health():
    return {"status": "ok"}


@app.get("/login", response_class=HTMLResponse)
async def login_page(request: Request):
    return templates.TemplateResponse(request, "login.html", {})


def _login_error(request: Request, message: str, username: str) -> HTMLResponse:
    return templates.TemplateResponse(
        request,
        "login.html",
        {"error": message, "username": username},
        status_code=401,
    )


@app.post("/login", response_class=HTMLResponse)
async def login(request: Request, username: str = Form(...), password: str = Form(...)):
    username = username.strip()
    if settings.allowed_users:
        if username.lower() not in settings.allowed_users:
            return _login_error(request, "Brukeren har ikke tilgang til tjenesten.", username)
    elif settings.fiken_enabled:
        return _login_error(
            request,
            "Tilgang er ikke konfigurert. Sett ALLOWED_USERS før innlogging.",
            username,
        )

    try:
        token_data = await zaptec_api.login(username, password)
    except ZaptecAuthError:
        return _login_error(request, "Feil e-post eller passord.", username)

    max_age = settings.session_max_age_seconds
    expires_in = token_data.get("expires_in")
    if isinstance(expires_in, (int, float)) and expires_in > 0:
        max_age = min(max_age, int(expires_in))

    session_id = secrets.token_urlsafe(32)
    database.create_session(session_id, token_data["access_token"], username, max_age)

    response = RedirectResponse(url="/", status_code=303)
    response.set_cookie(
        SESSION_COOKIE,
        session_id,
        max_age=max_age,
        httponly=True,
        secure=settings.cookie_secure,
        samesite="lax",
    )
    return response


@app.post("/logout")
async def logout(request: Request):
    session_id = request.cookies.get(SESSION_COOKIE)
    if session_id:
        database.delete_session(session_id)
    return _redirect_to_login()


# --- usage ----------------------------------------------------------------


async def load_installations(token: str) -> list[dict]:
    installations = await zaptec_api.get_installations(token)
    return sorted(installations, key=lambda i: (i.get("Name") or "").lower())


async def load_usage(
    token: str, installation: dict, year: int, month: int
) -> InstallationUsage:
    from_date, to_date = month_range(year, month)
    installation_id = installation.get("Id")
    sessions = await zaptec_api.get_chargehistory(
        token, installation_id, from_date, to_date
    )
    return InstallationUsage(
        installation_id=installation_id,
        name=installation.get("Name") or installation_id,
        sessions=sessions,
        total_kwh=total_kwh(sessions),
    )


async def load_all_usage(token: str, year: int, month: int) -> list[InstallationUsage]:
    return [
        await load_usage(token, installation, year, month)
        for installation in await load_installations(token)
    ]


def usage_pdf(usage: InstallationUsage, year: int, month: int, unit_price_ore: int) -> bytes:
    nok_per_kwh = unit_price_ore / 100
    return generate_invoice_pdf(
        usage.installation_id,
        usage.name,
        year,
        month,
        usage.sessions,
        usage.total_kwh,
        nok_per_kwh,
        round(usage.total_kwh * nok_per_kwh, 2),
    )


async def load_customers() -> dict[int, dict]:
    if fiken_api is None:
        return {}
    customers = await fiken_api.list_customers()
    return {c["contactId"]: c for c in customers}


# --- overview -------------------------------------------------------------


@app.get("/", response_class=HTMLResponse)
async def overview(
    request: Request,
    year: int | None = Query(None),
    month: int | None = Query(None),
    nok_per_kwh: str | None = Query(None),
    token: str = Depends(get_token),
):
    if year is None or month is None:
        year, month = previous_month(date.today())
    validate_period(year, month)

    usages = await load_all_usage(token, year, month)
    mappings = database.get_customer_mappings()
    drafts = database.get_drafts(year, month)

    customers: dict[int, dict] = {}
    fiken_error = None
    try:
        customers = await load_customers()
    except FikenError:
        log.exception("Could not load Fiken customers")
        fiken_error = "Kunne ikke hente kunder fra Fiken."

    rows = []
    for usage in usages:
        contact_id = mappings.get(usage.installation_id)
        draft = drafts.get(usage.installation_id)
        rows.append(
            {
                "usage": usage,
                "contact_id": contact_id,
                "customer": customers.get(contact_id) if contact_id else None,
                "draft": draft,
                "status": STATUSES.get(draft.status) if draft else None,
            }
        )

    # Only count a customer as ready when Fiken still knows it.
    ready = sum(1 for r in rows if r["usage"].total_kwh > 0 and r["customer"])
    missing = sum(1 for r in rows if r["usage"].total_kwh > 0 and not r["customer"])
    pending = any(r["draft"] and r["draft"].status == "pending" for r in rows)

    return templates.TemplateResponse(
        request,
        "home.html",
        {
            "user": request.state.session["user"],
            "year": year,
            "month": month,
            "nok_per_kwh": nok_per_kwh or "",
            "rows": rows,
            "ready": ready,
            "missing": missing,
            "pending": pending,
            "fiken_enabled": settings.fiken_enabled,
            "fiken_error": fiken_error,
            "years": range(date.today().year, 2019, -1),
        },
    )


# --- PDF export -----------------------------------------------------------


@app.post("/export")
async def export_usage(
    installation_id: str = Form(...),
    year: int = Form(...),
    month: int = Form(...),
    nok_per_kwh: str = Form(...),
    token: str = Depends(get_token),
):
    validate_period(year, month)
    unit_price_ore = nok_to_ore(parse_nok_per_kwh(nok_per_kwh))
    installation = next(
        (i for i in await load_installations(token) if i.get("Id") == installation_id),
        None,
    )
    if installation is None:
        raise PageError("Fant ikke installasjonen.", 404)

    usage = await load_usage(token, installation, year, month)
    return Response(
        usage_pdf(usage, year, month, unit_price_ore),
        media_type="application/pdf",
        headers={
            "Content-Disposition": f'attachment; filename="{pdf_filename(year, month, usage.name)}"'
        },
    )


@app.post("/exportall")
async def export_all_usage(
    year: int = Form(...),
    month: int = Form(...),
    nok_per_kwh: str = Form(...),
    token: str = Depends(get_token),
):
    validate_period(year, month)
    unit_price_ore = nok_to_ore(parse_nok_per_kwh(nok_per_kwh))
    usages = await load_all_usage(token, year, month)

    zip_buffer = io.BytesIO()
    with zipfile.ZipFile(zip_buffer, "w", zipfile.ZIP_DEFLATED) as zip_file:
        overview_rows = []
        for usage in usages:
            filename = pdf_filename(year, month, usage.name)
            zip_file.writestr(filename, usage_pdf(usage, year, month, unit_price_ore))
            overview_rows.append(
                {
                    "installasjons_navn": usage.name,
                    "installasjons_id": usage.installation_id,
                    "filnavn": filename,
                    "sum_kwh": usage.total_kwh,
                    "sum_kroner": round(usage.total_kwh * unit_price_ore / 100, 2),
                }
            )
        df = pl.DataFrame(
            overview_rows,
            schema={
                "installasjons_navn": pl.String,
                "installasjons_id": pl.String,
                "filnavn": pl.String,
                "sum_kwh": pl.Float64,
                "sum_kroner": pl.Float64,
            },
        )
        excel_buffer = io.BytesIO()
        df.write_excel(excel_buffer)
        csv_buffer = io.BytesIO()
        df.write_csv(csv_buffer)
        zip_file.writestr(f"{year}_{month:02d}_grunnlag_oversikt.xlsx", excel_buffer.getvalue())
        zip_file.writestr(f"{year}_{month:02d}_grunnlag_oversikt.csv", csv_buffer.getvalue())

    zip_buffer.seek(0)
    return StreamingResponse(
        zip_buffer,
        media_type="application/zip",
        headers={"Content-Disposition": f'attachment; filename="{year}_{month:02d}.zip"'},
    )


# --- Fiken drafts ---------------------------------------------------------


def require_fiken() -> FikenAPI:
    if fiken_api is None:
        raise PageError("Fiken er ikke konfigurert (FIKEN_API_TOKEN og FIKEN_COMPANY_SLUG).", 503)
    return fiken_api


@app.post("/drafts")
async def create_drafts(
    background_tasks: BackgroundTasks,
    year: int = Form(...),
    month: int = Form(...),
    nok_per_kwh: str = Form(...),
    token: str = Depends(get_token),
):
    fiken = require_fiken()
    validate_period(year, month)
    price = parse_nok_per_kwh(nok_per_kwh)
    if price <= 0:
        raise PageError("Pris per kWh må være større enn 0.")
    unit_price_ore = nok_to_ore(price)

    usages = await load_all_usage(token, year, month)
    try:
        customers = await load_customers()
    except FikenError:
        log.exception("Could not load Fiken customers")
        raise PageError("Kunne ikke hente kunder fra Fiken. Prøv igjen senere.", 502)
    # A mapping to a customer that is deleted or no longer a customer in Fiken
    # counts as missing, so no draft is made for the wrong contact.
    mappings = {
        installation_id: contact_id
        for installation_id, contact_id in database.get_customer_mappings().items()
        if contact_id in customers
    }
    items = plan_drafts(usages, mappings, year, month, unit_price_ore)
    if items:
        background_tasks.add_task(
            run_draft_job, fiken, settings, items, year, month, unit_price_ore
        )
    return RedirectResponse(overview_url(year, month, nok_per_kwh), status_code=303)


# --- customer mapping -----------------------------------------------------


async def find_installation(token: str, installation_id: str) -> dict:
    for installation in await load_installations(token):
        if installation.get("Id") == installation_id:
            return installation
    raise PageError("Fant ikke installasjonen.", 404)


def _matches(customer: dict, q: str) -> bool:
    haystack = " ".join(
        str(customer.get(k) or "")
        for k in ("name", "email", "organizationNumber", "customerNumber")
    ).lower()
    return all(part in haystack for part in q.lower().split())


@app.get("/installations/{installation_id}/customer", response_class=HTMLResponse)
async def customer_picker(
    request: Request,
    installation_id: str,
    year: int = Query(...),
    month: int = Query(...),
    nok_per_kwh: str | None = Query(None),
    q: str = Query(""),
    token: str = Depends(get_token),
):
    fiken = require_fiken()
    validate_period(year, month)
    installation = await find_installation(token, installation_id)
    customers = await fiken.list_customers()
    current_id = database.get_customer_mappings().get(installation_id)
    current = next((c for c in customers if c["contactId"] == current_id), None)
    matches = [c for c in customers if not c.get("inactive") and _matches(c, q.strip())]
    matches.sort(key=lambda c: (c.get("name") or "").lower())

    return templates.TemplateResponse(
        request,
        "customer.html",
        {
            "user": request.state.session["user"],
            "installation": installation,
            "current_id": current_id,
            "current": current,
            "customers": matches,
            "q": q,
            "year": year,
            "month": month,
            "nok_per_kwh": nok_per_kwh or "",
            "back_url": overview_url(year, month, nok_per_kwh),
        },
    )


@app.post("/installations/{installation_id}/customer")
async def set_customer(
    installation_id: str,
    contact_id: int = Form(...),
    year: int = Form(...),
    month: int = Form(...),
    nok_per_kwh: str = Form(""),
    token: str = Depends(get_token),
):
    fiken = require_fiken()
    validate_period(year, month)
    await find_installation(token, installation_id)
    customers = await fiken.list_customers()
    if not any(c["contactId"] == contact_id for c in customers):
        raise PageError("Fant ikke kunden i Fiken.", 404)
    database.set_customer_mapping(installation_id, contact_id)
    return RedirectResponse(overview_url(year, month, nok_per_kwh), status_code=303)


@app.post("/installations/{installation_id}/customer/delete")
async def delete_customer(
    installation_id: str,
    year: int = Form(...),
    month: int = Form(...),
    nok_per_kwh: str = Form(""),
    token: str = Depends(get_token),
):
    validate_period(year, month)
    await find_installation(token, installation_id)
    database.delete_customer_mapping(installation_id)
    return RedirectResponse(overview_url(year, month, nok_per_kwh), status_code=303)

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
from fiken_oauth import (
    FikenNotConnected,
    FikenOAuth,
    FikenOAuthError,
    FikenTokens,
    new_pkce_pair,
)
from invoice import format_month_to_norwegian, format_norwegian_accounting, generate_invoice_pdf
from zaptec_api import ZaptecAPI, ZaptecAuthError, ZaptecError

log = logging.getLogger(__name__)

SESSION_COOKIE = "session_id"
OAUTH_COOKIE = "fiken_oauth"
PUBLIC_PATHS = ("/static", "/login", "/health")

zaptec_api = ZaptecAPI(settings.zaptec_api_url)

# Tests inject an httpx.MockTransport here (and reset _fiken_tokens).
fiken_transport = None
_fiken_tokens: FikenTokens | None = None


def get_fiken_tokens() -> FikenTokens | None:
    global _fiken_tokens
    if not settings.fiken_enabled:
        return None
    if _fiken_tokens is None:
        oauth = FikenOAuth(
            settings.fiken_oauth_url,
            settings.fiken_client_id,
            settings.fiken_client_secret,
            settings.fiken_redirect_uri,
            transport=fiken_transport,
        )
        _fiken_tokens = FikenTokens(oauth, settings.secret_key)
    return _fiken_tokens


def fiken_client(user: str, company_slug: str | None = None) -> FikenAPI:
    tokens = get_fiken_tokens()
    if tokens is None:
        raise PageError("Fiken er ikke konfigurert.", 503)
    return FikenAPI(
        settings.fiken_api_url,
        lambda force: tokens.access_token(user, force),
        company_slug,
        transport=fiken_transport,
    )


@asynccontextmanager
async def lifespan(app: FastAPI):
    database.init_db(settings.database_path)
    database.delete_expired_sessions()
    database.fail_unfinished_drafts()
    if settings.fiken_client_id and not settings.fiken_enabled:
        log.warning(
            "FIKEN_CLIENT_ID is set, but FIKEN_CLIENT_SECRET, FIKEN_REDIRECT_URI "
            "or SECRET_KEY is missing: Fiken is disabled."
        )
    yield


app = FastAPI(lifespan=lifespan)
app.mount("/static", StaticFiles(directory=BASE_DIR / "static"), name="static")
templates = Jinja2Templates(directory=BASE_DIR / "templates")
templates.env.filters["nok"] = lambda ore: format_norwegian_accounting(ore / 100)
templates.env.filters["month_name"] = lambda m: (format_month_to_norwegian(m) or "").capitalize()
templates.env.globals["fiken_configured"] = lambda: settings.fiken_enabled


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


class FikenSetupRequired(Exception):
    """Fiken is configured, but this user hasn't connected or chosen a company."""


@app.exception_handler(FikenSetupRequired)
async def fiken_setup_handler(request: Request, exc: FikenSetupRequired):
    return RedirectResponse(url="/fiken", status_code=303)


@app.exception_handler(FikenNotConnected)
async def fiken_not_connected_handler(request: Request, exc: FikenNotConnected):
    return RedirectResponse(url="/fiken?error=expired", status_code=303)


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


def get_user(request: Request) -> str:
    return request.state.session["user"]


def require_company(user: str) -> tuple[FikenAPI, str]:
    """Return a Fiken client for the user's chosen company, or send them to /fiken."""
    if not settings.fiken_enabled:
        raise PageError("Fiken er ikke konfigurert.", 503)
    connection = database.get_fiken_connection(user)
    if connection is None or not connection.company_slug:
        raise FikenSetupRequired()
    return fiken_client(user, connection.company_slug), connection.company_slug


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
    if settings.allowed_users and username.lower() not in settings.allowed_users:
        return _login_error(request, "Brukeren har ikke tilgang til tjenesten.", username)

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


async def load_customers(fiken: FikenAPI) -> dict[int, dict]:
    customers = await fiken.list_customers()
    return {c["contactId"]: c for c in customers}


# --- overview -------------------------------------------------------------


@app.get("/", response_class=HTMLResponse)
async def overview(
    request: Request,
    year: int | None = Query(None),
    month: int | None = Query(None),
    nok_per_kwh: str | None = Query(None),
    token: str = Depends(get_token),
    user: str = Depends(get_user),
):
    if year is None or month is None:
        year, month = previous_month(date.today())
    validate_period(year, month)

    usages = await load_all_usage(token, year, month)

    connection = database.get_fiken_connection(user) if settings.fiken_enabled else None
    company_slug = connection.company_slug if connection else None
    mappings: dict[str, int] = {}
    drafts: dict[str, database.DraftRecord] = {}
    customers: dict[int, dict] = {}
    fiken_error = None
    if company_slug:
        mappings = database.get_customer_mappings(company_slug)
        drafts = database.get_drafts(company_slug, year, month)
        try:
            customers = await load_customers(fiken_client(user, company_slug))
        except FikenNotConnected:
            connection = company_slug = None
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
            "user": user,
            "year": year,
            "month": month,
            "nok_per_kwh": nok_per_kwh or "",
            "rows": rows,
            "ready": ready,
            "missing": missing,
            "pending": pending,
            "fiken_enabled": settings.fiken_enabled,
            # Connected with a company chosen: drafts can be created.
            "fiken_ready": bool(company_slug),
            "fiken_company": connection.company_name if company_slug else None,
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


@app.post("/drafts")
async def create_drafts(
    background_tasks: BackgroundTasks,
    year: int = Form(...),
    month: int = Form(...),
    nok_per_kwh: str = Form(...),
    token: str = Depends(get_token),
    user: str = Depends(get_user),
):
    fiken, company_slug = require_company(user)
    validate_period(year, month)
    price = parse_nok_per_kwh(nok_per_kwh)
    if price <= 0:
        raise PageError("Pris per kWh må være større enn 0.")
    unit_price_ore = nok_to_ore(price)

    usages = await load_all_usage(token, year, month)
    try:
        customers = await load_customers(fiken)
    except FikenError:
        log.exception("Could not load Fiken customers")
        raise PageError("Kunne ikke hente kunder fra Fiken. Prøv igjen senere.", 502)
    # A mapping to a customer that is deleted or no longer a customer in Fiken
    # counts as missing, so no draft is made for the wrong contact.
    mappings = {
        installation_id: contact_id
        for installation_id, contact_id in database.get_customer_mappings(company_slug).items()
        if contact_id in customers
    }
    items = plan_drafts(company_slug, usages, mappings, year, month, unit_price_ore)
    if items:
        background_tasks.add_task(
            run_draft_job, fiken, settings, company_slug, items, year, month, unit_price_ore
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
    user: str = Depends(get_user),
):
    fiken, company_slug = require_company(user)
    validate_period(year, month)
    installation = await find_installation(token, installation_id)
    customers = await fiken.list_customers()
    current_id = database.get_customer_mappings(company_slug).get(installation_id)
    current = next((c for c in customers if c["contactId"] == current_id), None)
    matches = [c for c in customers if not c.get("inactive") and _matches(c, q.strip())]
    matches.sort(key=lambda c: (c.get("name") or "").lower())

    return templates.TemplateResponse(
        request,
        "customer.html",
        {
            "user": user,
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
    user: str = Depends(get_user),
):
    fiken, company_slug = require_company(user)
    validate_period(year, month)
    await find_installation(token, installation_id)
    customers = await fiken.list_customers()
    if not any(c["contactId"] == contact_id for c in customers):
        raise PageError("Fant ikke kunden i Fiken.", 404)
    database.set_customer_mapping(company_slug, installation_id, contact_id)
    return RedirectResponse(overview_url(year, month, nok_per_kwh), status_code=303)


@app.post("/installations/{installation_id}/customer/delete")
async def delete_customer(
    installation_id: str,
    year: int = Form(...),
    month: int = Form(...),
    nok_per_kwh: str = Form(""),
    token: str = Depends(get_token),
    user: str = Depends(get_user),
):
    _, company_slug = require_company(user)
    validate_period(year, month)
    await find_installation(token, installation_id)
    database.delete_customer_mapping(company_slug, installation_id)
    return RedirectResponse(overview_url(year, month, nok_per_kwh), status_code=303)


# --- Fiken connection (OAuth) --------------------------------------------

FIKEN_PAGE_ERRORS = {
    "denied": "Du avbrøt tilkoblingen i Fiken. Ingenting ble endret.",
    "expired": "Tilkoblingen til Fiken er utløpt eller trukket tilbake. Koble til på nytt.",
    "state": "Tilkoblingen kunne ikke bekreftes. Prøv igjen.",
    "no_company": "Fiken-brukeren har ikke gitt appen tilgang til noen foretak.",
    "failed": "Fiken avviste tilkoblingen. Prøv igjen.",
}


def _require_fiken_enabled() -> FikenTokens:
    tokens = get_fiken_tokens()
    if tokens is None:
        raise PageError("Fiken er ikke konfigurert på serveren.", 503)
    return tokens


@app.get("/fiken", response_class=HTMLResponse)
async def fiken_settings(
    request: Request, error: str | None = Query(None), user: str = Depends(get_user)
):
    _require_fiken_enabled()
    connection = database.get_fiken_connection(user)
    return templates.TemplateResponse(
        request,
        "fiken.html",
        {
            "user": user,
            "connection": connection,
            "error": FIKEN_PAGE_ERRORS.get(error or ""),
        },
    )


@app.post("/fiken/connect")
async def fiken_connect(user: str = Depends(get_user)):
    tokens = _require_fiken_enabled()
    state = secrets.token_urlsafe(32)
    verifier, challenge = new_pkce_pair()
    response = RedirectResponse(tokens.oauth.authorize_url(state, challenge), status_code=303)
    # SameSite=Lax is sent on the top-level redirect back from fiken.no.
    response.set_cookie(
        OAUTH_COOKIE,
        f"{state}.{verifier}",
        max_age=600,
        path="/fiken",
        httponly=True,
        secure=settings.cookie_secure,
        samesite="lax",
    )
    return response


def _fiken_redirect(error: str | None = None, to: str = "/fiken") -> RedirectResponse:
    response = RedirectResponse(f"{to}?error={error}" if error else to, status_code=303)
    response.delete_cookie(OAUTH_COOKIE, path="/fiken")
    return response


@app.get("/fiken/callback")
async def fiken_callback(
    request: Request,
    code: str | None = Query(None),
    state: str | None = Query(None),
    error: str | None = Query(None),
    user: str = Depends(get_user),
):
    tokens = _require_fiken_enabled()
    if error:
        return _fiken_redirect("denied" if error == "access_denied" else "failed")

    expected_state, _, verifier = (request.cookies.get(OAUTH_COOKIE) or "").partition(".")
    if not (code and state and expected_state and verifier) or not secrets.compare_digest(
        state, expected_state
    ):
        return _fiken_redirect("state")

    try:
        token_set = await tokens.oauth.exchange_code(code, verifier, state)
    except FikenOAuthError:
        log.exception("Fiken code exchange failed")
        return _fiken_redirect("failed")
    tokens.save(user, token_set)

    companies = await fiken_client(user).list_companies()
    if not companies:
        return _fiken_redirect("no_company")
    if len(companies) == 1 and companies[0].get("hasApiAccess", True):
        database.set_fiken_company(user, companies[0]["slug"], companies[0]["name"])
        return _fiken_redirect(to="/")
    return _fiken_redirect(to="/fiken/company")


@app.get("/fiken/company", response_class=HTMLResponse)
async def fiken_company_picker(request: Request, user: str = Depends(get_user)):
    _require_fiken_enabled()
    connection = database.get_fiken_connection(user)
    if connection is None:
        return RedirectResponse("/fiken", status_code=303)
    companies = await fiken_client(user).list_companies()
    companies.sort(key=lambda c: (c.get("name") or "").lower())
    return templates.TemplateResponse(
        request,
        "fiken_company.html",
        {"user": user, "connection": connection, "companies": companies},
    )


@app.post("/fiken/company")
async def fiken_select_company(slug: str = Form(...), user: str = Depends(get_user)):
    _require_fiken_enabled()
    if database.get_fiken_connection(user) is None:
        return RedirectResponse("/fiken", status_code=303)
    companies = await fiken_client(user).list_companies()
    company = next((c for c in companies if c["slug"] == slug), None)
    if company is None:
        raise PageError("Fant ikke foretaket i Fiken.", 404)
    database.set_fiken_company(user, company["slug"], company["name"])
    return RedirectResponse("/", status_code=303)


@app.post("/fiken/disconnect")
async def fiken_disconnect(user: str = Depends(get_user)):
    tokens = _require_fiken_enabled()
    await tokens.disconnect(user)
    return RedirectResponse("/fiken", status_code=303)

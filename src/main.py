import io
import re
import secrets
import zipfile
from datetime import datetime

import httpx
from fastapi import Depends, FastAPI, Form, Request, Response
from fastapi.responses import HTMLResponse, RedirectResponse, StreamingResponse
from fastapi.security import OAuth2PasswordRequestForm
from fastapi.templating import Jinja2Templates

from database import create_session, delete_session, get_session
from invoice import generate_invoice_pdf
from zaptec_api import ZaptecAPI

app = FastAPI()
templates = Jinja2Templates(directory="templates")

ZAPTEC_API_URL = "https://api.zaptec.com"
TOKEN_URL = f"{ZAPTEC_API_URL}/oauth/token"

zaptec_api = ZaptecAPI(ZAPTEC_API_URL)


@app.middleware("http")
async def auth_middleware(request: Request, call_next):
    if request.url.path.startswith("/static"):
        return await call_next(request)

    session_id = request.cookies.get("session_id")

    if request.url.path.startswith("/login"):
        if session_id:
            return RedirectResponse(url="/", status_code=307)
        return await call_next(request)

    session_data = get_session(session_id)
    if not session_data:
        return RedirectResponse(url="/login", status_code=307)

    response = await call_next(request)
    return response


async def get_token(request: Request) -> str | None:
    session_id = request.cookies.get("session_id")
    if not session_id:
        return None

    session_data = get_session(session_id)
    if not session_data:
        return

    return session_data[0]  # access_token


@app.get("/login", response_class=HTMLResponse)
async def login_page(request: Request):
    return templates.TemplateResponse("login.html", {"request": request})


@app.post("/login", response_class=HTMLResponse)
async def login(request: Request, form_data: OAuth2PasswordRequestForm = Depends()):
    async with httpx.AsyncClient() as client:
        response = await client.post(
            TOKEN_URL,
            data={
                "grant_type": "password",
                "username": form_data.username,
                "password": form_data.password,
            },
        )

    if response.status_code != 200:
        return templates.TemplateResponse(
            "login.html", {"request": request, "error": "Invalid credentials"}
        )

    token_data = response.json()
    session_id = secrets.token_urlsafe(32)

    create_session(session_id, token_data["access_token"], form_data.username)

    response = RedirectResponse(url="/", status_code=303)
    response.set_cookie("session_id", session_id, httponly=True)
    return response


@app.get("/", response_class=HTMLResponse)
async def get_home(request: Request, token: str = Depends(get_token)):
    return templates.TemplateResponse(
        "home.html",
        {
            "request": request,
            "year": datetime.now().year,
            "month": datetime.now().month,
        },
    )


@app.post("/export", response_class=HTMLResponse)
async def export_usage(
    request: Request,
    installation_id: str = Form(...),
    installation_name: str = Form(...),
    year: int = Form(...),
    month: int = Form(...),
    nok_per_kwh: float = Form(...),
    token: str = Depends(get_token),
):
    if not (1 <= month <= 12) or year < 2000:
        return templates.TemplateResponse(
            "installations.html", {"request": request, "error": "Invalid date"}
        )
    if nok_per_kwh < 0:
        return templates.TemplateResponse(
            "installations.html", {"request": request, "error": "Invalid NOK per kWh"}
        )

    from_date = f"{year}-{month:02d}-01T00:00:00Z"

    if month == 12:
        to_date = f"{year + 1}-01-01T00:00:00Z"
    else:
        to_date = f"{year}-{month + 1:02d}-01T00:00:00Z"

    try:
        sessions_data = await zaptec_api.get_chargehistory(
            token,
            installation_id,
            from_date,
            to_date,
        )
    except Exception as e:
        return templates.TemplateResponse(
            "installations.html",
            {"request": request, "error": str(e)},
        )

    total_kwh = sum(session.get("Energy", 0) for session in sessions_data)
    total_cost = total_kwh * nok_per_kwh

    pdf_bytes = generate_invoice_pdf(
        installation_id,
        installation_name,
        year,
        month,
        sessions_data,
        total_kwh,
        nok_per_kwh,
        total_cost,
    )

    return Response(
        pdf_bytes,
        media_type="application/pdf",
        headers={
            "Content-Disposition": f'attachment;filename="{year}_{month:02d}_grunnlag_{make_safe_filename(installation_name)}.pdf"'
        },
    )


@app.get("/logout")
async def logout(request: Request):
    session_id = request.cookies.get("session_id")
    if session_id:
        delete_session(session_id)

    response = RedirectResponse(url="/login", status_code=303)
    response.delete_cookie("session_id")
    return response


def make_safe_filename(filename):
    safe_filename = re.sub(r'[<>:"/\\|?*]', "_", filename)
    safe_filename = safe_filename.strip(" .").replace(" ", "_")
    return safe_filename


@app.post("/exportall", response_class=HTMLResponse)
async def export_all_usage(
    request: Request,
    year: int = Form(...),
    month: int = Form(...),
    nok_per_kwh: float = Form(...),
    token: str = Depends(get_token),
):
    if not (1 <= month <= 12) or year < 2000:
        return templates.TemplateResponse(
            "installations.html", {"request": request, "error": "Invalid date"}
        )
    if nok_per_kwh < 0:
        return templates.TemplateResponse(
            "installations.html", {"request": request, "error": "Invalid NOK per kWh"}
        )

    from_date = f"{year}-{month:02d}-01T00:00:00Z"

    if month == 12:
        to_date = f"{year + 1}-01-01T00:00:00Z"
    else:
        to_date = f"{year}-{month + 1:02d}-01T00:00:00Z"

    try:
        installations = await zaptec_api.get_installations(token)
    except Exception as e:
        return templates.TemplateResponse(
            "login.html", {"request": request, "error": str(e)}
        )

    pdfs: list[dict] = []

    zip_buffer = io.BytesIO()
    with zipfile.ZipFile(zip_buffer, "w", zipfile.ZIP_DEFLATED) as zip_file:
        for installation in installations:
            installation_id = installation.get("Id")
            installation_name = installation.get("Name")
            try:
                sessions_data = await zaptec_api.get_chargehistory(
                    token,
                    installation_id,
                    from_date,
                    to_date,
                )
            except Exception:
                continue

            total_kwh = sum(session.get("Energy", 0) for session in sessions_data)
            total_cost = total_kwh * nok_per_kwh

            pdf_bytes = generate_invoice_pdf(
                installation_id,
                installation_name,
                year,
                month,
                sessions_data,
                total_kwh,
                nok_per_kwh,
                total_cost,
            )
            pdfs.append(
                {
                    "filename": f"{year}_{month:02d}_grunnlag_{make_safe_filename(installation_name)}.pdf",
                    "bytes": pdf_bytes,
                }
            )
            zip_file.writestr(
                f"{year}_{month:02d}_grunnlag_{make_safe_filename(installation_name)}.pdf",
                pdf_bytes,
            )
    zip_buffer.seek(0)
    return StreamingResponse(
        zip_buffer,
        media_type="application/zip",
        headers={"Content-Disposition": f"attachment; filename={year}_{month:02d}.zip"},
    )

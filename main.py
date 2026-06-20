from fastapi import FastAPI, Request, Form, Depends, HTTPException
from fastapi.responses import HTMLResponse, RedirectResponse, StreamingResponse
from starlette.templating import Jinja2Templates
from starlette.middleware.sessions import SessionMiddleware
from sqlalchemy.orm import Session
from sqlalchemy import func
from datetime import datetime
import json, os, csv, io, re

from database import get_db, init_db
from models import Survey, Question, SurveyResponse
from questions import QUESTIONS

app = FastAPI(title="Survey App")
app.add_middleware(
    SessionMiddleware,
    secret_key=os.getenv("SECRET_KEY", "survey-secret-key-change-in-prod-abc123"),
)

templates = Jinja2Templates(directory="templates")

ADMIN_USERNAME = os.getenv("ADMIN_USERNAME", "ovosound")
ADMIN_PASSWORD = os.getenv("ADMIN_PASSWORD", "12341007")


def slugify(s: str) -> str:
    s = s.lower().strip()
    s = re.sub(r"[^\w\s-]", "", s)
    s = re.sub(r"[\s_]+", "-", s)
    s = re.sub(r"-+", "-", s).strip("-")
    return s or "survey"


# ── Startup ────────────────────────────────────────────────────────────────────

@app.on_event("startup")
async def startup():
    init_db()
    _seed_default_survey()


def _seed_default_survey():
    from database import SessionLocal
    db = SessionLocal()
    try:
        if db.query(Survey).count() == 0:
            s = Survey(
                title="Consumer Shopping Trends",
                slug="shopping-trends",
                description="A 13-question survey about consumer shopping habits.",
                is_active=True,
                created_at=datetime.utcnow(),
            )
            db.add(s)
            db.flush()
            for i, q in enumerate(QUESTIONS):
                db.add(Question(
                    survey_id=s.id,
                    text=q["text"],
                    options=json.dumps(q["options"]),
                    page=q["page"],
                    order=i,
                ))
            db.commit()
    finally:
        db.close()


# ── Survey (public) ────────────────────────────────────────────────────────────

@app.get("/", response_class=RedirectResponse)
async def root(db: Session = Depends(get_db)):
    s = db.query(Survey).filter(Survey.is_active == True).first()
    return RedirectResponse(url=f"/s/{s.slug}" if s else "/no-surveys")


@app.get("/no-surveys", response_class=HTMLResponse)
async def no_surveys(request: Request):
    return templates.TemplateResponse(request, "no_surveys.html", {})


@app.get("/s/{slug}", response_class=RedirectResponse)
async def survey_root(slug: str):
    return RedirectResponse(url=f"/s/{slug}/1")


@app.get("/s/{slug}/info", response_class=HTMLResponse)
async def survey_info_get(request: Request, slug: str, next: int = 1, db: Session = Depends(get_db)):
    s = db.query(Survey).filter(Survey.slug == slug, Survey.is_active == True).first()
    if not s:
        raise HTTPException(status_code=404, detail="Survey not found or inactive")
    total_questions = db.query(func.count(Question.id)).filter(Question.survey_id == s.id).scalar() or 0
    return templates.TemplateResponse(request, "s/info.html", {
        "survey": s, "next": next, "errors": [], "data": {}, "total_questions": total_questions,
    })


@app.post("/s/{slug}/info")
async def survey_info_post(request: Request, slug: str, db: Session = Depends(get_db)):
    s = db.query(Survey).filter(Survey.slug == slug, Survey.is_active == True).first()
    if not s:
        raise HTTPException(status_code=404)

    form = await request.form()
    data = {
        "first_name": form.get("first_name", "").strip(),
        "last_name": form.get("last_name", "").strip(),
        "contact": form.get("contact", "").strip(),
    }
    next_raw = form.get("next", "1")
    next_page = int(next_raw) if str(next_raw).isdigit() else 1

    errors = []
    if not data["first_name"]:
        errors.append("First name is required")
    if not data["last_name"]:
        errors.append("Last name is required")

    if errors:
        total_questions = db.query(func.count(Question.id)).filter(Question.survey_id == s.id).scalar() or 0
        return templates.TemplateResponse(request, "s/info.html", {
            "survey": s, "next": next_page, "errors": errors, "data": data,
            "total_questions": total_questions,
        }, status_code=422)

    request.session[f"resp_{slug}"] = data
    return RedirectResponse(url=f"/s/{slug}/{next_page}", status_code=303)


@app.get("/s/{slug}/done", response_class=HTMLResponse)
async def survey_done(request: Request, slug: str, db: Session = Depends(get_db)):
    s = db.query(Survey).filter(Survey.slug == slug).first()
    if not s:
        raise HTTPException(status_code=404)
    return templates.TemplateResponse(request, "s/done.html", {"survey": s})


@app.get("/s/{slug}/{page}", response_class=HTMLResponse)
async def survey_get(request: Request, slug: str, page: int, db: Session = Depends(get_db)):
    s = db.query(Survey).filter(Survey.slug == slug, Survey.is_active == True).first()
    if not s:
        raise HTTPException(status_code=404, detail="Survey not found or inactive")

    if not request.session.get(f"resp_{slug}"):
        return RedirectResponse(url=f"/s/{slug}/info?next={page}")

    total_pages = db.query(func.max(Question.page)).filter(Question.survey_id == s.id).scalar() or 1
    if page < 1 or page > total_pages:
        return RedirectResponse(url=f"/s/{slug}/1")

    questions = db.query(Question).filter(
        Question.survey_id == s.id, Question.page == page
    ).order_by(Question.order).all()
    for q in questions:
        q.options_list = json.loads(q.options)

    return templates.TemplateResponse(request, "s/survey.html", {
        "survey": s,
        "page": page,
        "total_pages": total_pages,
        "questions": questions,
        "saved": request.session.get(f"ans_{slug}", {}),
        "errors": [],
    })


@app.post("/s/{slug}/{page}")
async def survey_post(request: Request, slug: str, page: int, db: Session = Depends(get_db)):
    s = db.query(Survey).filter(Survey.slug == slug, Survey.is_active == True).first()
    if not s:
        raise HTTPException(status_code=404)

    resp_info = request.session.get(f"resp_{slug}")
    if not resp_info:
        return RedirectResponse(url=f"/s/{slug}/info?next={page}", status_code=303)

    total_pages = db.query(func.max(Question.page)).filter(Question.survey_id == s.id).scalar() or 1
    questions = db.query(Question).filter(
        Question.survey_id == s.id, Question.page == page
    ).order_by(Question.order).all()
    for q in questions:
        q.options_list = json.loads(q.options)

    form = await request.form()
    sess_key = f"ans_{slug}"
    answers = request.session.get(sess_key, {})
    errors = []

    for q in questions:
        key = f"q{q.id}"
        val = form.get(key, "").strip()
        if val:
            answers[key] = val
        else:
            errors.append(q.id)

    if errors:
        return templates.TemplateResponse(request, "s/survey.html", {
            "survey": s, "page": page, "total_pages": total_pages,
            "questions": questions, "saved": answers, "errors": errors,
        }, status_code=422)

    request.session[sess_key] = answers

    if page < total_pages:
        return RedirectResponse(url=f"/s/{slug}/{page + 1}", status_code=303)

    db.add(SurveyResponse(
        survey_id=s.id,
        answers=json.dumps(answers),
        first_name=resp_info.get("first_name"),
        last_name=resp_info.get("last_name"),
        contact=resp_info.get("contact") or None,
        ip_address=request.client.host,
        created_at=datetime.utcnow(),
    ))
    db.commit()
    request.session[sess_key] = {}
    request.session.pop(f"resp_{slug}", None)
    return RedirectResponse(url=f"/s/{slug}/done", status_code=303)


# ── Admin Auth ─────────────────────────────────────────────────────────────────

def is_admin(request: Request) -> bool:
    return bool(request.session.get("admin_logged_in"))


@app.get("/admin/login", response_class=HTMLResponse)
async def admin_login_get(request: Request):
    if is_admin(request):
        return RedirectResponse(url="/admin")
    return templates.TemplateResponse(request, "admin/login.html", {"error": None})


@app.post("/admin/login")
async def admin_login_post(request: Request, username: str = Form(...), password: str = Form(...)):
    if username == ADMIN_USERNAME and password == ADMIN_PASSWORD:
        request.session["admin_logged_in"] = True
        return RedirectResponse(url="/admin", status_code=303)
    return templates.TemplateResponse(request, "admin/login.html", {"error": "Invalid credentials"})


@app.get("/admin/logout")
async def admin_logout(request: Request):
    request.session.pop("admin_logged_in", None)
    return RedirectResponse(url="/admin/login", status_code=303)


# ── Admin Dashboard ────────────────────────────────────────────────────────────

@app.get("/admin", response_class=HTMLResponse)
async def admin_dashboard(request: Request, db: Session = Depends(get_db)):
    if not is_admin(request):
        return RedirectResponse(url="/admin/login")

    surveys = db.query(Survey).order_by(Survey.created_at.desc()).all()
    total_responses = db.query(func.count(SurveyResponse.id)).scalar() or 0

    survey_stats = []
    for s in surveys:
        count = db.query(func.count(SurveyResponse.id)).filter(
            SurveyResponse.survey_id == s.id).scalar() or 0
        q_count = db.query(func.count(Question.id)).filter(
            Question.survey_id == s.id).scalar() or 0
        last = db.query(SurveyResponse).filter(
            SurveyResponse.survey_id == s.id
        ).order_by(SurveyResponse.created_at.desc()).first()
        survey_stats.append({
            "survey": s,
            "response_count": count,
            "question_count": q_count,
            "last_response": last.created_at if last else None,
        })

    recent = db.query(SurveyResponse).order_by(
        SurveyResponse.created_at.desc()).limit(8).all()
    surveys_map = {s.id: s for s in surveys}
    for r in recent:
        r.answers_dict = json.loads(r.answers)
        r.survey_obj = surveys_map.get(r.survey_id)

    return templates.TemplateResponse(request, "admin/dashboard.html", {
        "survey_stats": survey_stats,
        "total_surveys": len(surveys),
        "active_surveys": sum(1 for s in surveys if s.is_active),
        "total_responses": total_responses,
        "recent": recent,
    })


# ── Admin Survey CRUD ──────────────────────────────────────────────────────────

def _qs_to_dicts(questions):
    return [{"text": q.text, "options": json.loads(q.options), "page": q.page, "order": q.order,
             "qtype": q.qtype or "choice"}
            for q in questions]


def _parse_questions_from_form(form) -> list:
    q_texts = form.getlist("q_text[]")
    q_pages = form.getlist("q_page[]")
    q_types = form.getlist("q_type[]")
    result = []
    for i, (txt, pg) in enumerate(zip(q_texts, q_pages)):
        qtype = q_types[i] if i < len(q_types) else "choice"
        opts = [o.strip() for o in form.getlist(f"q_options_{i}[]") if o.strip()]
        if txt.strip() and (qtype == "text" or opts):
            result.append({"text": txt.strip(), "options": opts, "qtype": qtype,
                           "page": int(pg) if str(pg).isdigit() else 1, "order": i})
    return result


@app.get("/admin/surveys/create", response_class=HTMLResponse)
async def survey_create_get(request: Request):
    if not is_admin(request):
        return RedirectResponse(url="/admin/login")
    return templates.TemplateResponse(request, "admin/survey_form.html", {
        "survey": None, "q_data": [], "errors": [], "action": "/admin/surveys/create",
    })


@app.post("/admin/surveys/create")
async def survey_create_post(request: Request, db: Session = Depends(get_db)):
    if not is_admin(request):
        return RedirectResponse(url="/admin/login")

    form = await request.form()
    title = form.get("title", "").strip()
    slug = form.get("slug", "").strip() or slugify(title)
    description = form.get("description", "").strip()
    is_active = form.get("is_active") == "on"
    q_data = _parse_questions_from_form(form)

    errors = []
    if not title:
        errors.append("Title is required")
    if db.query(Survey).filter(Survey.slug == slug).first():
        errors.append(f"Slug '{slug}' already taken")
    if not q_data:
        errors.append("Add at least one question")

    if errors:
        return templates.TemplateResponse(request, "admin/survey_form.html", {
            "survey": {"title": title, "slug": slug, "description": description, "is_active": is_active},
            "q_data": q_data, "errors": errors, "action": "/admin/surveys/create",
        })

    s = Survey(title=title, slug=slug, description=description,
               is_active=is_active, created_at=datetime.utcnow())
    db.add(s)
    db.flush()
    for q in q_data:
        db.add(Question(survey_id=s.id, text=q["text"], qtype=q.get("qtype", "choice"),
                        options=json.dumps(q["options"]), page=q["page"], order=q["order"]))
    db.commit()
    return RedirectResponse(url="/admin", status_code=303)


@app.get("/admin/surveys/{sid}/edit", response_class=HTMLResponse)
async def survey_edit_get(request: Request, sid: int, db: Session = Depends(get_db)):
    if not is_admin(request):
        return RedirectResponse(url="/admin/login")
    s = db.query(Survey).filter(Survey.id == sid).first()
    if not s:
        raise HTTPException(status_code=404)
    qs = db.query(Question).filter(Question.survey_id == sid).order_by(Question.page, Question.order).all()
    return templates.TemplateResponse(request, "admin/survey_form.html", {
        "survey": s, "q_data": _qs_to_dicts(qs), "errors": [], "action": f"/admin/surveys/{sid}/edit",
    })


@app.post("/admin/surveys/{sid}/edit")
async def survey_edit_post(request: Request, sid: int, db: Session = Depends(get_db)):
    if not is_admin(request):
        return RedirectResponse(url="/admin/login")
    s = db.query(Survey).filter(Survey.id == sid).first()
    if not s:
        raise HTTPException(status_code=404)

    form = await request.form()
    title = form.get("title", "").strip()
    slug = form.get("slug", "").strip() or slugify(title)
    description = form.get("description", "").strip()
    is_active = form.get("is_active") == "on"
    q_data = _parse_questions_from_form(form)

    errors = []
    if not title:
        errors.append("Title is required")
    if db.query(Survey).filter(Survey.slug == slug, Survey.id != sid).first():
        errors.append(f"Slug '{slug}' already taken")
    if not q_data:
        errors.append("Add at least one question")

    if errors:
        return templates.TemplateResponse(request, "admin/survey_form.html", {
            "survey": {"id": sid, "title": title, "slug": slug,
                       "description": description, "is_active": is_active},
            "q_data": q_data, "errors": errors, "action": f"/admin/surveys/{sid}/edit",
        })

    s.title, s.slug, s.description, s.is_active = title, slug, description, is_active
    db.query(Question).filter(Question.survey_id == sid).delete()
    for q in q_data:
        db.add(Question(survey_id=s.id, text=q["text"], qtype=q.get("qtype", "choice"),
                        options=json.dumps(q["options"]), page=q["page"], order=q["order"]))
    db.commit()
    return RedirectResponse(url="/admin", status_code=303)


@app.post("/admin/surveys/{sid}/delete")
async def survey_delete(request: Request, sid: int, db: Session = Depends(get_db)):
    if not is_admin(request):
        return RedirectResponse(url="/admin/login")
    s = db.query(Survey).filter(Survey.id == sid).first()
    if s:
        db.query(Question).filter(Question.survey_id == sid).delete()
        db.query(SurveyResponse).filter(SurveyResponse.survey_id == sid).delete()
        db.delete(s)
        db.commit()
    return RedirectResponse(url="/admin", status_code=303)


@app.post("/admin/surveys/{sid}/toggle")
async def survey_toggle(request: Request, sid: int, db: Session = Depends(get_db)):
    if not is_admin(request):
        return RedirectResponse(url="/admin/login")
    s = db.query(Survey).filter(Survey.id == sid).first()
    if s:
        s.is_active = not s.is_active
        db.commit()
    return RedirectResponse(url="/admin", status_code=303)


# ── Admin Survey Responses & Export ───────────────────────────────────────────

@app.get("/admin/surveys/{sid}/responses", response_class=HTMLResponse)
async def admin_survey_responses(request: Request, sid: int, db: Session = Depends(get_db)):
    if not is_admin(request):
        return RedirectResponse(url="/admin/login")
    s = db.query(Survey).filter(Survey.id == sid).first()
    if not s:
        raise HTTPException(status_code=404)

    qs = db.query(Question).filter(Question.survey_id == sid).order_by(Question.page, Question.order).all()
    responses = db.query(SurveyResponse).filter(
        SurveyResponse.survey_id == sid
    ).order_by(SurveyResponse.created_at.desc()).all()

    for r in responses:
        r.answers_dict = json.loads(r.answers)

    stats = {}
    for q in qs:
        qtype = q.qtype or "choice"
        if qtype == "text":
            text_answers = [r.answers_dict.get(f"q{q.id}") for r in responses
                            if r.answers_dict.get(f"q{q.id}")]
            stats[q.id] = {"question": q, "qtype": "text", "text_answers": text_answers,
                           "total": len(text_answers), "counts": {}}
        else:
            opts = json.loads(q.options)
            counts = {opt: 0 for opt in opts}
            total = 0
            for r in responses:
                val = r.answers_dict.get(f"q{q.id}")
                if val and val in counts:
                    counts[val] += 1
                    total += 1
            stats[q.id] = {"question": q, "qtype": "choice", "counts": counts, "total": total}

    return templates.TemplateResponse(request, "admin/survey_responses.html", {
        "survey": s, "questions": qs, "responses": responses, "stats": stats, "total": len(responses),
    })


@app.get("/admin/surveys/{sid}/export")
async def admin_survey_export(request: Request, sid: int, db: Session = Depends(get_db)):
    if not is_admin(request):
        return RedirectResponse(url="/admin/login")
    s = db.query(Survey).filter(Survey.id == sid).first()
    if not s:
        raise HTTPException(status_code=404)

    qs = db.query(Question).filter(Question.survey_id == sid).order_by(Question.page, Question.order).all()
    responses = db.query(SurveyResponse).filter(
        SurveyResponse.survey_id == sid
    ).order_by(SurveyResponse.created_at.desc()).all()

    output = io.StringIO()
    writer = csv.writer(output)
    writer.writerow(
        ["id", "first_name", "last_name", "contact", "created_at", "ip"]
        + [f"Q{q.id}: {q.text[:40]}" for q in qs]
    )
    for r in responses:
        ad = json.loads(r.answers)
        writer.writerow(
            [r.id, r.first_name or "", r.last_name or "", r.contact or "",
             r.created_at.isoformat(), r.ip_address]
            + [ad.get(f"q{q.id}", "") for q in qs]
        )
    output.seek(0)
    return StreamingResponse(
        iter([output.getvalue()]), media_type="text/csv",
        headers={"Content-Disposition": f"attachment; filename={s.slug}-responses.csv"},
    )


# ── Legacy redirects ───────────────────────────────────────────────────────────

@app.get("/survey/{page}")
async def legacy_survey_redirect(page: int, db: Session = Depends(get_db)):
    s = db.query(Survey).filter(Survey.is_active == True).first()
    return RedirectResponse(url=f"/s/{s.slug}/{page}" if s else "/")


@app.get("/admin/responses")
async def legacy_responses_redirect():
    return RedirectResponse(url="/admin")


@app.get("/admin/export")
async def legacy_export_redirect(db: Session = Depends(get_db)):
    s = db.query(Survey).filter(Survey.is_active == True).first()
    return RedirectResponse(url=f"/admin/surveys/{s.id}/export" if s else "/admin")

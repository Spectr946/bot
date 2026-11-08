import csv
import hashlib
import hmac
import io
import json
import os
import secrets
import threading
import time
from collections import defaultdict, deque
from datetime import datetime, timezone
from pathlib import Path
from urllib.parse import quote
from zoneinfo import ZoneInfo

from fastapi import FastAPI, Form, HTTPException, Request, UploadFile
from fastapi.responses import HTMLResponse, PlainTextResponse, RedirectResponse, Response
from fastapi.staticfiles import StaticFiles
from fastapi.templating import Jinja2Templates
from starlette.middleware.sessions import SessionMiddleware
from generation import MAX_FILE_BYTES, MAX_UPLOAD_MB

ROOT = Path(__file__).resolve().parent
MAX_UPLOAD=  MAX_FILE_BYTES
MAX_REQUEST_BYTES = MAX_UPLOAD + 1024 * 1024
STATUS_NAMES = {
    "draft": "На проверке", "published": "Опубликовано", "closed": "Закрыто", "deleted": "Удалено",
    "pending": "Ожидает решения", "approved": "Одобрен", "rejected": "Отклонён", "redeemed": "Использован",
}
ACTION_NAMES = {
    "create_group": "Создание класса", "set_user_group": "Назначение класса",
    "add_book": "Загрузка учебника", "delete_book": "Удаление учебника", "create_quiz": "Создание работы",
    "publish_quiz": "Публикация работы", "close_quiz": "Закрытие работы", "update_settings": "Изменение расписания",
    "update_quiz_questions": "Редактирование вопросов", "buy": "Покупка сертификата", "resolve_request": "Решение по сертификату",
}

def local_time(value):
    if not value:
        return "Нет данных"
    try:
        moment  = datetime.fromisoformat(str(value).replace("Z", "+00:00"))
        if moment.tzinfo is None:
            moment = moment.replace(tzinfo=timezone.utc)
        return moment.astimezone(ZoneInfo("Europe/Moscow")).strftime("%d.%m.%Y %H:%M")
    except (TypeError, ValueError):
        return "Нет данных"

class RequestBodyTooLarge(HTTPException):
    pass

def request_body_limit(app, max_bytes):
    async def handle_request(scope, receive, send):
        if scope["type"] != "http":
            return await app(scope, receive, send)
        received =  0

        async def bounded_receive():
            nonlocal received
            message = await receive()
            if message["type"] == "http.request":
                received += len(message.get("body", b""))
                if received > max_bytes:
                    raise RequestBodyTooLarge(413, f"Файл слишком большой. Максимум — {MAX_UPLOAD_MB} МБ.")
            return message

        await app(scope, bounded_receive, send)
    return handle_request

def create_app(service, ingestion=None):
    password = os.environ.get("ADMIN_PASSWORD", "")
    username = os.environ.get("ADMIN_USERNAME", "admin")
    if not password:
        raise RuntimeError("Задайте ADMIN_PASSWORD перед запуском панели администратора.")
    if len(password) < 12:
        raise RuntimeError("ADMIN_PASSWORD должен содержать не менее 12 символов.")
    session_secret=  os.environ.get("SESSION_SECRET") or hmac.new(
        password.encode(), b"study-bot-admin-session-v1", hashlib.sha256
    ).hexdigest()
    app = FastAPI(title="Учебный бот — управление", docs_url=None, redoc_url=None, openapi_url=None)
    app.add_middleware(
        SessionMiddleware,
        secret_key=session_secret,
        session_cookie="study_admin",
        max_age  = 8 * 60 * 60,
        same_site="strict",
        https_only=os.environ.get("COOKIE_SECURE", "false").lower() in {"1", "true", "yes"},
    )
    app.add_middleware(request_body_limit, max_bytes=MAX_REQUEST_BYTES)

    @app.exception_handler(RequestBodyTooLarge)
    async def body_too_large(unused_request, exc):
        return PlainTextResponse(exc.detail, status_code=413)

    app.mount("/static", StaticFiles(directory=str(ROOT / "static")), name="static")
    templates = Jinja2Templates(directory=str(ROOT / "templates"))
    templates.env.filters["local_time"] = local_time
    def status_name(value):
        return STATUS_NAMES.get(value, value or "—")

    def pretty_json(value):
        return json.dumps(value, ensure_ascii=False, sort_keys=True)

    def action_name(value):
        return ACTION_NAMES.get(value, value)

    def upload_stage(value):
        names = {
            "queued": "Ожидает обработки", "reading": "Чтение файла",
            "ocr": "Распознавание текста", "checking_topics": "Проверка и очистка тем",
            "completed": "Текст сохранён", "failed": "Ошибка обработки",
        }
        return names.get(value, "Извлечение и распознавание текста")

    templates.env.filters["status_name"] = status_name
    templates.env.filters["pretty_json"] = pretty_json
    templates.env.filters["action_name"] = action_name
    templates.env.filters["upload_stage"] = upload_stage
    attempts = defaultdict(deque)
    attempts_lock =  threading.Lock()

    @app.middleware("http")
    async def security_headers(request: Request, call_next):
        length = request.headers.get("content-length", "0")
        if not length.isdecimal():
            response = PlainTextResponse("Некорректная длина запроса.", status_code=400)
        elif len(length) > 20 or int(length) > MAX_REQUEST_BYTES:
            response = PlainTextResponse(f"Файл слишком большой. Максимум — {MAX_UPLOAD_MB} МБ.", status_code=413)
        else:
            response = await call_next(request)
        response.headers["X-Content-Type-Options"] = "nosniff"
        response.headers["X-Frame-Options"] = "DENY"
        response.headers["Referrer-Policy"] = "same-origin"
        response.headers["Content-Security-Policy"] = "default-src 'none'; style-src 'self'; img-src 'self' data:; form-action 'self'; base-uri 'none'; frame-ancestors 'none'"
        if request.url.path.startswith("/admin"):
            response.headers["Cache-Control"] = "no-store"
        return response

    def csrf(request):
        if "csrf" not in request.session:
            request.session["csrf"] = secrets.token_urlsafe(32)
        return request.session["csrf"]

    def require_auth(request):
        if not request.session.get("authenticated"):
            raise HTTPException(status_code=303, headers={"Location": "/admin/login"})

    def verify_csrf(request, token):
        expected = request.session.get("csrf", "")
        if not expected or not hmac.compare_digest(str(token), str(expected)):
            raise HTTPException(403, "Форма устарела. Обновите страницу и повторите действие.")

    def authorize(request, token):
        require_auth(request)
        verify_csrf(request, token)

    def page(request, template, status_code=200, **context):
        return templates.TemplateResponse(
            request=request,
            name  = template,
            context={
                "csrf": csrf(request),
                "notice": request.session.pop("notice", None),
                "error": request.session.pop("error", None),
                "path": request.url.path, **context,
            },
            status_code=status_code,
        )

    def redirect(request, target, message=None):
        if message:
            request.session["notice"] = message
        return RedirectResponse(target, status_code=303)

    def fail(request, target, exc):
        if isinstance(exc, (ValueError, LookupError)):
            request.session["error"] = str(exc)[:700]
        else:
            request.session["error"] = "Что-то не получилось. Попробуй ещё раз или проверь настройки бота."
        return redirect(request, target)

    @app.get("/")
    def index():
        return RedirectResponse("/admin", status_code=303)

    @app.get("/admin/login", response_class=HTMLResponse)
    def login_page(request: Request):
        if request.session.get("authenticated"):
            return redirect(request, "/admin")
        return page(request, "login.html", title="Вход учителя")

    @app.post("/admin/login")
    def login(request: Request, username_input: str = Form(alias="username"), password_input: str = Form(alias="password"), csrf_token: str = Form()):
        verify_csrf(request, csrf_token)
        key = request.client.host if request.client else "unknown"
        now =  time.monotonic()
        with attempts_lock:
            for stale_key in list(attempts):
                while attempts[stale_key] and attempts[stale_key][0] < now - 600:
                    attempts[stale_key].popleft()
                if not attempts[stale_key]:
                    del attempts[stale_key]
            if len(attempts[key]) >= 5:
                return page(request, "login.html", title="Вход в панель", error="Слишком много попыток входа. Подожди 10 минут и попробуй ещё.", status_code=429)
            valid_user = hmac.compare_digest(username_input.encode(), username.encode())
            valid_password = hmac.compare_digest(password_input.encode(), password.encode())
            if not (valid_user and valid_password):
                attempts[key].append(now)
                return page(request, "login.html", title="Вход в панель", error="Неверное имя пользователя или пароль.", status_code=401)
            attempts.pop(key, None)
        request.session.clear()
        request.session.update({"authenticated": True, "csrf": secrets.token_urlsafe(32)})
        return redirect(request, "/admin")

    @app.post("/admin/logout")
    def logout(request: Request, csrf_token: str = Form()):
        authorize(request, csrf_token)
        request.session.clear()
        return redirect(request, "/admin/login")

    @app.get("/admin", response_class=HTMLResponse)
    def dashboard(request: Request):
        require_auth(request)
        return redirect(request, "/admin/books")

    @app.get("/admin/users", response_class=HTMLResponse)
    def users_page(request: Request, group_id: str = ""):
        require_auth(request)
        users = service.student_summaries()
        groups=  service.list_groups()
        counts = {}
        for group in groups:
            counts[str(group["id"])] = 0
        unassigned_count = 0
        selected_users = []
        for user in users:
            user_group = user.get("group_id")
            if user_group is None:
                unassigned_count += 1
                if group_id == "unassigned":
                    selected_users.append(user)
            else:
                counts[str(user_group)] += 1
                if str(user_group) == group_id:
                    selected_users.append(user)
        selected_group = None
        if group_id and group_id != "unassigned":
            for group in groups:
                if str(group["id"]) == group_id:
                    selected_group = group
                    break
            if selected_group is None:
                raise HTTPException(404, "Класс не найден.")
        return page(request, "users.html", title="Ученики", users=selected_users, groups=groups,
                    selected_group =  selected_group, selected_group_id=group_id, counts=counts,
                    unassigned_count=unassigned_count)

    @app.post("/admin/groups")
    def create_group(request: Request, name: str = Form(), csrf_token: str = Form()):
        authorize(request, csrf_token)
        try:
            service.create_group(name.strip(), actor="admin")
            return redirect(request, "/admin/users", "Класс создан.")
        except Exception as exc:
            return fail(request, "/admin/users", exc)

    @app.post("/admin/users/{user_id}")
    def change_user(request: Request, user_id: str, name: str = Form(), group_id: str = Form(""), csrf_token: str = Form()):
        authorize(request, csrf_token)
        target = "/admin/users?group_id=" + (quote(group_id, safe="") if group_id else "unassigned")
        try:
            group = int(group_id) if group_id else None
            service.update_student(user_id, name.strip(), group, actor="admin")
            return redirect(request, target, "ФИО и класс сохранены.")
        except Exception as exc:
            return fail(request, "/admin/users", exc)

    @app.get("/admin/books", response_class=HTMLResponse)
    def books_page(request: Request):
        require_auth(request)
        return page(request, "books.html", title="Учебники", books=service.list_books(), subjects=service.list_subjects())

    @app.post("/admin/subjects")
    def add_subject(request: Request, name: str = Form(), csrf_token: str = Form()):
        authorize(request, csrf_token)
        try:
            service.add_subject(name.strip(), actor="admin")
            return redirect(request, "/admin/books", "Предмет добавлен.")
        except Exception as exc:
            return fail(request, "/admin/books", exc)

    @app.post("/admin/subjects/{subject_id}/delete")
    def delete_subject(request: Request, subject_id: int, csrf_token: str = Form()):
        authorize(request, csrf_token)
        try:
            service.delete_subject(subject_id, actor="admin")
            return redirect(request, "/admin/books", "Предмет удалён из справочника.")
        except Exception as exc:
            return fail(request, "/admin/books", exc)

    @app.get("/admin/books/{book_id}", response_class=HTMLResponse)
    def book_page(request: Request, book_id: int):
        require_auth(request)
        try:
            book=  service.get_book(book_id)
            topic_review = service.book_topic_status(book_id)
            topics = topic_review["topics"]
        except (ValueError, LookupError) as error:
            raise HTTPException(404, str(error)) from None
        return page(request, "book.html", title=book["title"], book=book, topics=topics, topic_review=topic_review)

    @app.post("/admin/books/{book_id}/review-topics")
    def review_book_topics(request: Request, book_id: int, csrf_token: str = Form()):
        authorize(request, csrf_token)
        target = f"/admin/books/{book_id}"
        try:
            review  = service.refresh_book_topics(book_id)
            message = "Темы проверены." if review["status"] == "reviewed" else "Лишние темы убрали. Проверка ИИ пока недоступна, можно попробовать позже."
            return redirect(request, target, message)
        except Exception as exc:
            return fail(request, target, exc)

    @app.get("/admin/uploads/{upload_id}", response_class=HTMLResponse)
    def upload_page(request: Request, upload_id: str):
        require_auth(request)
        try:
            job = ingestion.get(upload_id) if ingestion is not None else None
        except (LookupError, ValueError):
            job = None
        if job is None:
            raise HTTPException(404, "Загрузка не найдена.")
        return page(request, "upload.html", title="Обработка учебника", upload=job)

    @app.post("/admin/uploads/{upload_id}/retry")
    def retry_upload(request: Request, upload_id: str, csrf_token: str = Form()):
        authorize(request, csrf_token)
        target =  f"/admin/uploads/{quote(upload_id, safe='')}"
        try:
            if ingestion is None:
                raise ValueError("Не выходит продолжить обработку. Загрузи учебник заново.")
            ingestion.retry(upload_id)
            return redirect(request, target, "Продолжим с тех страниц которые уже сохранили.")
        except Exception as exc:
            return fail(request, target, exc)

    @app.post("/admin/books")
    def upload_book(request: Request, title: str = Form(), subject: str = Form(), file: UploadFile = Form(), csrf_token: str = Form(), force_ocr: str = Form("")):
        authorize(request, csrf_token)
        try:
            from generation import extract_text
            subjects = []
            for item in service.list_subjects():
                subjects.append(item["name"])
            if subject.strip() not in subjects:
                raise ValueError("Выберите предмет из справочника.")
            if Path(file.filename or "").suffix.lower() not in {".txt", ".pdf", ".docx", ".png", ".jpg", ".jpeg", ".webp", ".tif", ".tiff"}:
                raise ValueError("Поддерживаются TXT, PDF, DOCX и изображения PNG, JPEG, WebP, TIFF.")
            use_ocr = force_ocr.lower() in {"1", "true", "yes", "on"}
            if ingestion is not None:
                job = ingestion.submit(file.file, file.filename or "", title.strip(), subject.strip(), force_ocr=use_ocr)
                upload_id = quote(str(job["id"]), safe="")
                return redirect(request, f"/admin/uploads/{upload_id}", "Файл принят. Теперь читаем учебник в фоне, это займет не много времени.")
            data=  file.file.read(MAX_UPLOAD + 1)
            if len(data) > MAX_UPLOAD:
                raise ValueError(f"Файл слишком большой. Максимум — {MAX_UPLOAD_MB} МБ.")
            text = extract_text(data, file.filename or "", force_ocr=True) if use_ocr else extract_text(data, file.filename or "")
            service.add_book(title.strip(), subject.strip(), text, actor="admin")
            return redirect(request, "/admin/books", "Учебник загружен. Теперь можно сделать квиз или тест по его теме.")
        except Exception as exc:
            return fail(request, "/admin/books", exc)
        finally:
            file.file.close()

    @app.post("/admin/books/{book_id}/delete")
    def delete_book(request: Request, book_id: int, confirmed: str = Form(""), csrf_token: str = Form()):
        authorize(request, csrf_token)
        if confirmed != "yes":
            return fail(request, "/admin/books", ValueError("Подтвердите удаление учебника."))
        try:
            service.delete_book(book_id, actor="admin")
            return redirect(request, "/admin/books", "Учебник удалён.")
        except Exception as exc:
            return fail(request, "/admin/books", exc)

    @app.get("/admin/quizzes", response_class=HTMLResponse)
    def quizzes_page(request: Request, book_id: int | None = None, topic: str = ""):
        require_auth(request)
        books = service.list_books()
        selected_book = books[0] if books else None
        for book in books:
            if book["id"] == book_id:
                selected_book = book
                break
        topics = service.book_topics(selected_book["id"]) if selected_book else []
        quizzes = []
        for quiz in service.list_quizzes():
            if quiz["kind"] == "assessment":
                quizzes.append(quiz)
        results = []
        for result in service.results():
            if result.get("kind") == "assessment":
                results.append(result)
        return page(request, "quizzes.html", title="Квизы", quizzes=quizzes,
                    books=books, groups=service.list_groups(), selected_book=selected_book,
                    topics=topics, selected_topic=topic[:250],
                    results=results[:20])

    @app.get("/admin/tests", response_class=HTMLResponse)
    def tests_page(request: Request, book_id: int | None = None, topic: str = ""):
        require_auth(request)
        books = service.list_books()
        selected_book = books[0] if books else None
        for book in books:
            if book["id"] == book_id:
                selected_book = book
                break
        topics=  service.book_topics(selected_book["id"]) if selected_book else []
        quizzes = []
        for quiz in service.list_quizzes():
            if quiz["kind"] in {"mini", "test"}:
                quizzes.append(quiz)
        return page(request, "tests.html", title="Тесты с монетами", settings=service.settings(),
                    books=books, groups=service.list_groups(), selected_book=selected_book,
                    topics=topics, selected_topic=topic[:250],
                    quizzes=quizzes)

    @app.post("/admin/tests")
    def create_test(request: Request, book_id: int = Form(), topic: str = Form(), group_id: str = Form(""), csrf_token: str = Form()):
        authorize(request, csrf_token)
        try:
            result  = service.create_quiz(book_id, topic.strip(), kind="test", count=10, group_id=int(group_id) if group_id else None, actor="admin")
            quiz_id = result["id"]
            return redirect(request, f"/admin/quizzes/{quiz_id}", "Тест создан.")
        except Exception as exc:
            return fail(request, "/admin/tests", exc)

    @app.post("/admin/quizzes")
    def create_quiz(request: Request, book_id: int = Form(), topic: str = Form(), count: int = Form(10), group_id: str = Form(""), csrf_token: str = Form()):
        authorize(request, csrf_token)
        try:
            result = service.create_quiz(book_id, topic.strip(), kind="assessment", count=count, group_id=int(group_id) if group_id else None, actor="admin")
            quiz_id = result["id"]
            return redirect(request, f"/admin/quizzes/{quiz_id}", "Черновик готов. Перед публикацией посмотри вопросы и правильные ответы.")
        except Exception as exc:
            return fail(request, "/admin/quizzes", exc)

    @app.get("/admin/quizzes/{quiz_id}", response_class=HTMLResponse)
    def quiz_page(request: Request, quiz_id: int):
        require_auth(request)
        try:
            quiz =  service.get_quiz(quiz_id)
        except (LookupError, ValueError) as exc:
            raise HTTPException(404, str(exc)) from exc
        results = []
        for result in service.results():
            if result.get("quiz_id") == quiz_id:
                results.append(result)
        return page(request, "quiz.html", title=quiz["topic"], quiz=quiz, results=results)

    @app.post("/admin/quizzes/{quiz_id}/publish")
    def publish_quiz(request: Request, quiz_id: int, reviewed: str = Form(""), csrf_token: str = Form()):
        authorize(request, csrf_token)
        try:
            if reviewed != "yes":
                raise ValueError("Подтвердите проверку вопросов и правильных ответов.")
            service.publish_quiz(quiz_id, actor="admin")
            return redirect(request, f"/admin/quizzes/{quiz_id}", "Работа опубликована. Ученики уже могут открыть её в боте.")
        except Exception as exc:
            return fail(request, f"/admin/quizzes/{quiz_id}", exc)

    @app.post("/admin/quizzes/{quiz_id}/edit")
    def edit_quiz(request: Request, quiz_id: int, question: list[str] = Form(), option: list[str] = Form(), answer: list[int] = Form(), explanation: list[str] = Form(), csrf_token: str = Form()):
        authorize(request, csrf_token)
        try:
            count = len(question)
            if not 1 <= count <= 30 or len(option) != count * 4 or len(answer) != count or len(explanation) != count:
                raise ValueError("Некорректная форма редактирования вопросов.")
            questions = []
            for index in range(count):
                options = []
                for value in option[index * 4:index * 4 + 4]:
                    options.append(value.strip())
                questions.append({"question": question[index].strip(), "options": options,
                                  "answer": answer[index], "explanation": explanation[index].strip()})
            service.update_quiz_questions(quiz_id, questions, actor="admin")
            return redirect(request, f"/admin/quizzes/{quiz_id}", "Вопросы сохранены. Ещё раз проверь правильные ответы перед публикацией.")
        except Exception as exc:
            return fail(request, f"/admin/quizzes/{quiz_id}", exc)

    @app.post("/admin/quizzes/{quiz_id}/close")
    def close_quiz(request: Request, quiz_id: int, csrf_token: str = Form()):
        authorize(request, csrf_token)
        try:
            service.close_quiz(quiz_id, actor="admin")
            return redirect(request, f"/admin/quizzes/{quiz_id}", "Работа закрыта для новых прохождений.")
        except Exception as exc:
            return fail(request, f"/admin/quizzes/{quiz_id}", exc)

    @app.post("/admin/quizzes/{quiz_id}/delete")
    def delete_quiz(request: Request, quiz_id: int, confirmed: str = Form(""), csrf_token: str = Form()):
        authorize(request, csrf_token)
        target=  "/admin/quizzes"
        try:
            quiz = service.get_quiz(quiz_id)
            target = "/admin/tests" if quiz["kind"] in {"mini", "test"} else "/admin/quizzes"
            if confirmed != "yes":
                raise ValueError("Подтвердите удаление.")
            service.delete_quiz(quiz_id, actor="admin")
            return redirect(request, target, "Удалено. Результаты учеников сохранены.")
        except Exception as exc:
            return fail(request, target, exc)

    @app.get("/admin/settings", response_class=HTMLResponse)
    def settings_page(request: Request):
        require_auth(request)
        return redirect(request, "/admin/tests")

    @app.post("/admin/settings")
    def update_settings(request: Request, trimester_start: str = Form(), trimester_end: str = Form(), active_week_parity: int = Form(), timezone: str = Form("Europe/Moscow"), max_active_weeks: int = Form(5), holidays: str = Form(""), mini_book_id: str = Form(""), mini_topic: str = Form(""), csrf_token: str = Form()):
        authorize(request, csrf_token)
        try:
            holiday_dates = []
            for item in holidays.replace(",", "\n").splitlines():
                if item.strip():
                    holiday_dates.append(item.strip())
            values = {"trimester_start": trimester_start, "trimester_end": trimester_end,
                      "active_week_parity": active_week_parity, "timezone": timezone.strip(), "max_active_weeks": max_active_weeks,
                      "holidays": holiday_dates,
                      "mini_book_id": int(mini_book_id) if mini_book_id else None, "mini_topic": mini_topic.strip()}
            service.update_settings(values, actor="admin")
            return redirect(request, "/admin/tests", "Расписание сохранили. Учебник и тема для ежедневных тестов тоже сохранены.")
        except Exception as exc:
            return fail(request, "/admin/tests", exc)

    @app.post("/admin/mini/prepare")
    def prepare_mini(request: Request, group_id: str = Form(""), csrf_token: str = Form()):
        authorize(request, csrf_token)
        try:
            quiz  = service.available_mini(group_id=int(group_id) if group_id else None)
            if not quiz:
                return redirect(request, "/admin/tests", "Сегодня тест не запланирован. Проверь учебник и тему, а ещё день по расписанию.")
            return redirect(request, f"/admin/quizzes/{quiz['id']}", "Открыт сегодняшний тест.")
        except Exception as exc:
            return fail(request, "/admin/tests", exc)

    @app.post("/admin/mini/settings")
    def update_mini_source(request: Request, book_id: str = Form(""), topic: str = Form(""), enabled: str = Form(""), csrf_token: str = Form()):
        authorize(request, csrf_token)
        try:
            if enabled and not book_id:
                raise ValueError("Выберите учебник для тестов.")
            values = {"mini_book_id": int(book_id) if enabled else None,
                      "mini_topic": topic.strip() if enabled else ""}
            service.update_settings(values, actor="admin")
            return redirect(request, "/admin/tests", "Настройки тестов сохранены.")
        except Exception as error:
            return fail(request, "/admin/tests", error)

    def filtered_results(quiz_id, group_id):
        results = []
        for item in service.results():
            if quiz_id and str(item.get("quiz_id")) != quiz_id:
                continue
            if group_id and str(item.get("group_id")) != group_id:
                continue
            results.append(item)
        return results

    @app.get("/admin/results", response_class=HTMLResponse)
    def results_page(request: Request, quiz_id: str = "", group_id: str = ""):
        require_auth(request)
        return page(request, "results.html", title="Результаты и ошибки", results=filtered_results(quiz_id, group_id),
                    quizzes=service.list_quizzes(), groups=service.list_groups(), selected_quiz=quiz_id, selected_group=group_id)

    @app.get("/admin/results.csv")
    def results_csv(request: Request, quiz_id: str = "", group_id: str = ""):
        require_auth(request)
        def safe(value):
            text = str(value if value is not None else "")
            return "'" + text if text.startswith(("\t", "\r", "\n")) or text.lstrip().startswith(("=", "+", "-", "@")) else text
        stream = io.StringIO(newline="")
        writer=  csv.writer(stream, delimiter=";")
        writer.writerow(["Дата", "ID ученика", "Ученик", "Класс", "Предмет", "Тема", "Тип", "Верно", "Всего", "Монеты", "Ошибки"])
        for result in filtered_results(quiz_id, group_id):
            errors = []
            for error in result.get("errors", []):
                errors.append(f"{error.get('index', 0) + 1}: {error.get('question', '')}; ответ: {error.get('selected', '')}; верно: {error.get('correct_answer', '')}")
            values = [result.get("finished_at", result.get("date", "")), result.get("user_id"), result.get("user_name"),
                      result.get("group_name"), result.get("subject"), result.get("topic"),
                      "Тест с монетами" if result.get("kind") in {"mini", "test"} else "Квиз",
                      result.get("score"), result.get("total"), result.get("coins"), " | ".join(errors)]
            row = []
            for value in values:
                row.append(safe(value))
            writer.writerow(row)
        return Response("\ufeff" + stream.getvalue(), media_type="text/csv; charset=utf-8", headers={"Content-Disposition": 'attachment; filename="study-results.csv"', "Cache-Control": "no-store"})

    @app.get("/admin/shop", response_class=HTMLResponse)
    def shop_page(request: Request):
        require_auth(request)
        requests = []
        for item in service.list_requests():
            if item["status"] in {"pending", "approved"}:
                requests.append(item)
        return page(request, "shop.html", title="Сертификаты", items=service.shop_catalog(), requests=requests)

    @app.post("/admin/shop")
    def update_certificates(request: Request, enabled_keys: list[str] = Form([]), csrf_token: str = Form()):
        authorize(request, csrf_token)
        try:
            service.set_enabled_certificates(enabled_keys, actor="admin")
            return redirect(request, "/admin/shop", "Сертификаты сохранены. Ученики смогут купить те что отмечены.")
        except Exception as error:
            return fail(request, "/admin/shop", error)

    @app.post("/admin/shop/add")
    def add_certificate(request: Request, title: str = Form(), price: int = Form(), stock: int = Form(), description: str = Form(""), csrf_token: str = Form()):
        authorize(request, csrf_token)
        try:
            service.add_certificate(title.strip(), price, stock, description.strip(), actor="admin")
            return redirect(request, "/admin/shop", "Сертификат добавлен.")
        except Exception as exc:
            return fail(request, "/admin/shop", exc)

    @app.post("/admin/shop/{key}/delete")
    def delete_certificate(request: Request, key: str, csrf_token: str = Form()):
        authorize(request, csrf_token)
        try:
            service.delete_certificate(key, actor="admin")
            return redirect(request, "/admin/shop", "Сертификат удалён.")
        except Exception as exc:
            return fail(request, "/admin/shop", exc)

    @app.post("/admin/requests/{request_id}")
    def resolve_request(request: Request, request_id: int, action: str = Form(), csrf_token: str = Form()):
        authorize(request, csrf_token)
        if action not in {"approve", "reject", "redeem"}:
            raise HTTPException(400, "Некорректное действие.")
        try:
            service.resolve_request(request_id, action, actor="admin")
            return redirect(request, "/admin/shop", "Статус сертификата обновлён.")
        except Exception as exc:
            return fail(request, "/admin/shop", exc)

    @app.get("/admin/audit", response_class=HTMLResponse)
    def audit_page(request: Request):
        require_auth(request)
        return page(request, "audit.html", title="Журнал действий", events=service.audit())

    @app.exception_handler(HTTPException)
    async def http_error(request: Request, exc: HTTPException):
        if exc.status_code == 303:
            return RedirectResponse(exc.headers.get("Location", "/admin/login"), status_code=303)
        return page(request, "error.html", title="Не удалось открыть страницу", message=str(exc.detail), status_code=exc.status_code)

    return app

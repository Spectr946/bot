from contextlib import contextmanager
from datetime import date, datetime, timedelta, timezone
import hashlib
import json
from pathlib import Path
import sqlite3
import threading
import time
import uuid
from zoneinfo import ZoneInfo

class DomainError(ValueError):
    pass

DEFAULT_SETTINGS=  {
    "trimester_start": "2026-11-02",
    "trimester_end": "2027-01-10",
    "active_week_parity": 0,
    "timezone": "Europe/Moscow",
    "max_active_weeks": 5,
    "holidays": [],
    "mini_book_id": None,
    "mini_topic": "",
    "enabled_certificates": ["grade5", "plus1", "retake", "homework"],
}

def current_time():
    return datetime.now(timezone.utc).isoformat(timespec="seconds")

def load_json(value, default=None):
    return json.loads(value) if value is not None else default

def json_text(value):
    return json.dumps(value, ensure_ascii=False, separators=(",", ":"))

def time_zone(name):
    if name == "Europe/Moscow":
        return timezone(timedelta(hours=3))
    if name in ("UTC", "Etc/UTC"):
        return timezone.utc
    try:
        return ZoneInfo(name)
    except Exception:
        raise DomainError("Неизвестный часовой пояс.")

class StudyService:
    def setup(self, db_path, generator=None):
        self.db_path = str(Path(db_path).resolve())
        self.generator = generator
        self.lock = threading.RLock()
        uri = Path(self.db_path).as_uri() + "?mode=rw"
        self.db = sqlite3.connect(uri, uri=True, timeout=60, check_same_thread=False, isolation_level=None)
        self.db.row_factory = sqlite3.Row
        self.db.setconfig(sqlite3.SQLITE_DBCONFIG_ENABLE_FKEY, True)
        return self

    def close(self):
        with self.lock:
            self.db.close()

    @contextmanager
    def transaction(self):
        with self.lock:
            self.db.execute("BEGIN IMMEDIATE")
            try:
                yield self.db
                self.db.commit()
            finally:
                if self.db.in_transaction:
                    self.db.rollback()

    @staticmethod
    def read_settings(db):
        row = db.execute("SELECT value FROM metadata WHERE key='settings'").fetchone()
        settings = DEFAULT_SETTINGS.copy()
        settings.update(load_json(row["value"], {}))
        return settings

    @staticmethod
    def add_audit(db, actor, action, details):
        db.execute("INSERT INTO audit_log(actor,action,details,created_at) VALUES(?,?,?,?)",
                   (str(actor), action, json_text(details), current_time()))

    @staticmethod
    def check_admin(db, actor):
        if str(actor) == "admin":
            return
        raise DomainError("Это действие доступно только в панели учителя.")

    @staticmethod
    def add_user_if_missing(db, user_id):
        user_id = str(user_id)
        if not user_id or len(user_id) > 100:
            raise DomainError("Некорректный идентификатор пользователя.")
        db.execute("INSERT OR IGNORE INTO users(id,created_at) VALUES(?,?)", (user_id, current_time()))
        return user_id

    @staticmethod
    def read_user(db, user_id):
        row=  db.execute("SELECT u.*,g.name AS group_name FROM users u LEFT JOIN groups g ON g.id=u.group_id WHERE u.id=?",
                         (str(user_id),)).fetchone()
        if row is None:
            raise DomainError("Пользователь не найден.")
        result = dict(row)
        result.pop("role", None)
        result["profile"] = load_json(result.get("profile"), {})
        result["user_id"] = result["id"]
        result["balance"] = result["coins"]
        result["full_name"] = result["name"]
        result["registered"] = bool(result["registered_at"])
        return result

    @staticmethod
    def parse_day(day, settings):
        if day is None:
            return datetime.now(time_zone(settings["timezone"])).date()
        if isinstance(day, datetime):
            return day.date()
        if isinstance(day, date):
            return day
        try:
            return date.fromisoformat(str(day))
        except Exception:
            raise DomainError("Дата должна быть в формате ГГГГ-ММ-ДД.")

    @staticmethod
    def eligible_day(day, settings):
        start = date.fromisoformat(settings["trimester_start"])
        end  = date.fromisoformat(settings["trimester_end"])
        anchor = start - timedelta(days=start.weekday())
        week = (day - anchor).days // 7
        parity = settings["active_week_parity"]
        return (start <= day <= end and day.weekday() < 5
                and day.isoformat() not in settings["holidays"]
                and week >= parity and week % 2 == parity
                and (week - parity) // 2 < settings["max_active_weeks"])

    def today(self):
        return self.parse_day(None, self.settings())

    def user(self, user_id, name="", chat_id=None):
        with self.transaction() as db:
            user_id =  self.add_user_if_missing(db, user_id)
            if name:
                db.execute("UPDATE users SET name=? WHERE id=? AND registered_at IS NULL", (str(name)[:300], user_id))
            if chat_id is not None:
                db.execute("UPDATE users SET chat_id=? WHERE id=?", (str(chat_id), user_id))
            return self.read_user(db, user_id)

    def list_users(self):
        with self.transaction() as db:
            users = []
            for row in db.execute("SELECT id FROM users ORDER BY name,id"):
                users.append(self.read_user(db, row["id"]))
            return users

    @staticmethod
    def check_student_name(name):
        name = " ".join(str(name).split())
        parts = name.split()
        valid = 2 <= len(parts) <= 5 and len(name) <= 200
        for part in parts:
            if not part[0].isalpha() or not part[-1].isalpha():
                valid = False
            for char in part:
                if not char.isalpha() and char not in "-'’":
                    valid = False
        if not valid:
            raise DomainError("Укажите фамилию, имя и отчество, если оно есть, без цифр и других знаков.")
        return name

    def validate_student_name(self, value):
        return self.check_student_name(value)

    @staticmethod
    def check_class_name(name):
        name = "".join(str(name).split()).upper()
        number = name
        if name and name[-1] in "АБВГДЕЁЖЗИЙКЛМНОПРСТУФХЦЧШЩЪЫЬЭЮЯABCDEFGHIJKLMNOPQRSTUVWXYZ":
            number = name[:-1]
        if number not in ("1", "2", "3", "4", "5", "6", "7", "8", "9", "10", "11", "12"):
            raise DomainError("Укажите класс, например 7А или 10Б.")
        return name

    def is_registered(self, user_id):
        with self.transaction() as db:
            row=  db.execute("SELECT registered_at FROM users WHERE id=?", (str(user_id),)).fetchone()
            return bool(row and row["registered_at"])

    def register_student(self, user_id, full_name, class_name):
        full_name = self.check_student_name(full_name)
        class_name = self.check_class_name(class_name)
        with self.transaction() as db:
            user_id = self.add_user_if_missing(db, user_id)
            user  = self.read_user(db, user_id)
            if user["registered"]:
                saved_class = "".join((user["group_name"] or "").split()).upper()
                if user["name"] == full_name and saved_class == class_name:
                    return user
                raise DomainError("Вы уже зарегистрированы. Исправить ФИО или класс может учитель.")
            group = None
            for row in db.execute("SELECT id,name FROM groups"):
                if "".join(row["name"].split()).upper() == class_name:
                    group = row
                    break
            group_id = group["id"] if group else db.execute("INSERT INTO groups(name) VALUES(?)", (class_name,)).lastrowid
            db.execute("UPDATE users SET name=?,group_id=?,registered_at=? WHERE id=?",
                       (full_name, group_id, current_time(), user_id))
            self.add_audit(db, user_id, "register_student", {"group_id": group_id})
            return self.read_user(db, user_id)

    def update_student(self, user_id, name, group_id, actor="admin"):
        name =  self.check_student_name(name)
        with self.transaction() as db:
            self.check_admin(db, actor)
            user_id = self.add_user_if_missing(db, user_id)
            group_id = self.read_group(db, group_id)
            if group_id is None:
                raise DomainError("Выберите класс ученика.")
            db.execute("UPDATE users SET name=?,group_id=?,registered_at=COALESCE(registered_at,?) WHERE id=?",
                       (name, group_id, current_time(), user_id))
            self.add_audit(db, actor, "update_student", {"user_id": user_id, "group_id": group_id})
            return self.read_user(db, user_id)

    def remember_profile(self, user_id, profile):
        if not isinstance(profile, dict):
            profile = {}
        limits=  {"first_name": 150, "last_name": 150, "username": 100, "user_locale": 35}
        with self.transaction() as db:
            user_id = self.add_user_if_missing(db, user_id)
            saved = self.read_user(db, user_id)["profile"]
            for field, limit in limits.items():
                value = profile.get(field)
                if isinstance(value, str) and value.strip():
                    cleaned = ""
                    for char in value.strip():
                        if char.isprintable():
                            cleaned += char
                    saved[field] = cleaned[:limit]
            last_activity  = profile.get("last_activity_time")
            if type(last_activity) is int and last_activity >= 0:
                saved["last_activity_time"] = last_activity
            full_name = (saved.get("first_name", "") + " " + saved.get("last_name", "")).strip()
            db.execute("UPDATE users SET profile=?,last_seen=?,name=CASE WHEN registered_at IS NULL AND ?!='' THEN ? ELSE name END WHERE id=?",
                       (json_text(saved), current_time(), full_name, full_name, user_id))
            return self.read_user(db, user_id)

    def student_summaries(self):
        results = self.results()
        summaries = {}
        for user in self.list_users():
            profile =  user["profile"]
            summaries[user["id"]] = {
                "id": user["id"], "name": user["name"] or user["id"],
                "username": profile.get("username", ""), "registered": user["registered"],
                "registered_at": user["registered_at"], "full_name": user["name"],
                "group_id": user["group_id"], "group_name": user["group_name"],
                "coins": user["coins"], "last_seen": user["last_seen"],
                "completed_count": 0, "average_percent": None, "weak_topics": [],
                "percentages": [], "mistakes_by_topic": {},
            }
        for result in results:
            summary = summaries.get(str(result["user_id"]))
            if summary is None:
                continue
            summary["completed_count"] += 1
            score = result["score"]
            total = result["total"]
            summary["percentages"].append(score / total * 100)
            topic = result["topic"]
            if score < total:
                mistakes = summary["mistakes_by_topic"]
                mistakes[topic] = mistakes.get(topic, 0) + total - score
        for summary in summaries.values():
            percentages = summary.pop("percentages")
            if percentages:
                summary["average_percent"] = round(sum(percentages) / len(percentages), 1)
            mistakes = summary.pop("mistakes_by_topic")
            ranked = []
            for topic in mistakes:
                ranked.append((-mistakes[topic], topic))
            ranked.sort()
            for count, topic in ranked[:3]:
                summary["weak_topics"].append(topic)
        return list(summaries.values())

    def list_groups(self):
        with self.transaction() as db:
            groups = []
            for row in db.execute("SELECT id,name FROM groups ORDER BY name,id"):
                groups.append(dict(row))
            return groups

    def create_group(self, name, actor="admin"):
        name  = str(name).strip()
        if not name or len(name) > 150:
            raise DomainError("Укажите название класса (до 150 символов).")
        with self.transaction() as db:
            self.check_admin(db, actor)
            if db.execute("SELECT id FROM groups WHERE name=?", (name,)).fetchone():
                raise DomainError("Класс с таким названием уже существует.")
            cursor = db.execute("INSERT INTO groups(name) VALUES(?)", (name,))
            self.add_audit(db, actor, "create_group", {"id": cursor.lastrowid, "name": name})
            return {"id": cursor.lastrowid, "name": name}

    @staticmethod
    def read_group(db, group_id):
        if group_id in (None, ""):
            return None
        try:
            group_id = int(group_id)
        except Exception:
            raise DomainError("Некорректный класс.")
        if not db.execute("SELECT id FROM groups WHERE id=?", (group_id,)).fetchone():
            raise DomainError("Класс не найден.")
        return group_id

    def set_user_group(self, user_id, group_id, actor="admin"):
        with self.transaction() as db:
            self.check_admin(db, actor)
            user_id = self.add_user_if_missing(db, user_id)
            group_id =  self.read_group(db, group_id)
            db.execute("UPDATE users SET group_id=? WHERE id=?", (group_id, user_id))
            self.add_audit(db, actor, "set_user_group", {"user_id": user_id, "group_id": group_id})
            return self.read_user(db, user_id)

    @staticmethod
    def clean_subject_name(name):
        name = " ".join(str(name).split())
        if not name or len(name) > 150:
            raise DomainError("Укажите название предмета до 150 символов.")
        for char in name:
            if not char.isprintable():
                raise DomainError("Укажите название предмета до 150 символов.")
        return name

    @staticmethod
    def save_subject(db, name):
        db.execute("INSERT INTO subjects(name,name_key) VALUES(?,?) ON CONFLICT(name_key) DO UPDATE SET archived=0",
                   (name, name.casefold()))
        return dict(db.execute("SELECT id,name FROM subjects WHERE name_key=?", (name.casefold(),)).fetchone())

    def list_subjects(self):
        with self.transaction() as db:
            subjects = []
            for row in db.execute("SELECT id,name FROM subjects WHERE archived=0 ORDER BY name,id"):
                subjects.append(dict(row))
            return subjects

    def add_subject(self, name, actor="admin"):
        name = self.clean_subject_name(name)
        with self.transaction() as db:
            self.check_admin(db, actor)
            subject = self.save_subject(db, name)
            self.add_audit(db, actor, "add_subject", subject)
            return subject

    def delete_subject(self, subject_id, actor="admin"):
        with self.transaction() as db:
            self.check_admin(db, actor)
            subject=  db.execute("SELECT id,name FROM subjects WHERE id=? AND archived=0", (subject_id,)).fetchone()
            if subject is None:
                raise DomainError("Предмет не найден.")
            db.execute("UPDATE subjects SET archived=1 WHERE id=?", (subject_id,))
            self.add_audit(db, actor, "delete_subject", dict(subject))

    @staticmethod
    def validate_book(title, subject, text):
        title, subject, text = str(title).strip(), str(subject).strip(), str(text).strip()
        if not title or not subject or not text:
            raise DomainError("Укажите название, предмет и текст учебника.")
        if len(title) > 300 or len(subject) > 150 or len(text) > 600000:
            raise DomainError("Учебник превышает допустимый размер.")
        return title, subject, text

    def add_book(self, title, subject, text, actor="admin"):
        title, subject, text = self.validate_book(title, subject, text)
        with self.transaction() as db:
            self.check_admin(db, actor)
            subject = self.save_subject(db, self.clean_subject_name(subject))["name"]
            cursor = db.execute("INSERT INTO books(title,subject,text,created_at) VALUES(?,?,?,?)",
                                (title, subject, text, current_time()))
            self.add_audit(db, actor, "add_book", {"id": cursor.lastrowid, "title": title})
            return self.read_book(db, cursor.lastrowid)

    @staticmethod
    def read_upload_job(db, job_id):
        row = db.execute("SELECT * FROM upload_jobs WHERE id=?", (str(job_id),)).fetchone()
        if row is None:
            raise DomainError("Загрузка учебника не найдена.")
        return dict(row)

    def get_upload_job(self, job_id):
        with self.transaction() as db:
            return self.read_upload_job(db, job_id)

    def list_upload_jobs(self, limit=20):
        limit  = min(max(int(limit), 1), 100)
        with self.transaction() as db:
            rows = db.execute("SELECT * FROM upload_jobs ORDER BY created_at DESC,rowid DESC LIMIT ?", (limit,))
            jobs = []
            for row in rows:
                jobs.append(dict(row))
            return jobs

    def count_pending_upload_jobs(self):
        with self.transaction() as db:
            return db.execute("SELECT count(*) FROM upload_jobs WHERE status IN ('queued','processing')").fetchone()[0]

    def failed_upload_ids(self, limit=5):
        with self.transaction() as db:
            jobs = []
            rows = db.execute("SELECT id FROM upload_jobs WHERE status='failed' ORDER BY updated_at DESC,rowid DESC LIMIT ?",
                              (max(0, int(limit)),))
            for row in rows:
                jobs.append(row["id"])
            return jobs

    def retry_upload_job(self, job_id, max_pending=5):
        with self.transaction() as db:
            job = self.read_upload_job(db, job_id)
            if job["status"] != "failed":
                raise DomainError("Повторить можно только неудачную загрузку.")
            pending = db.execute("SELECT count(*) FROM upload_jobs WHERE status IN ('queued','processing')").fetchone()[0]
            if pending >= max_pending:
                raise DomainError("В очереди уже 5 учебников. Дождитесь завершения обработки.")
            db.execute("UPDATE upload_jobs SET status='queued',stage='queued',error=NULL,updated_at=? WHERE id=?",
                       (current_time(), str(job_id)))
            return self.read_upload_job(db, job_id)

    def create_upload_job(self, job_id, title, subject, filename, force_ocr=False, max_pending=5):
        title, subject, _ = self.validate_book(title, subject, "pending")
        if not isinstance(job_id, str) or len(job_id) != 32:
            raise DomainError("Некорректный идентификатор загрузки.")
        for char in job_id:
            if char not in "0123456789abcdef":
                raise DomainError("Некорректный идентификатор загрузки.")
        with self.transaction() as db:
            pending =  db.execute("SELECT count(*) FROM upload_jobs WHERE status IN ('queued','processing')").fetchone()[0]
            if pending >= max_pending:
                raise DomainError("В очереди уже 5 учебников. Дождитесь завершения обработки.")
            now = current_time()
            db.execute("INSERT INTO upload_jobs(id,title,subject,filename,force_ocr,created_at,updated_at) VALUES(?,?,?,?,?,?,?)",
                       (job_id, title, subject, filename, int(bool(force_ocr)), now, now))
            return self.read_upload_job(db, job_id)

    def recover_upload_jobs(self):
        with self.transaction() as db:
            db.execute("UPDATE upload_jobs SET status='queued',stage='queued',updated_at=? WHERE status='processing'",
                       (current_time(),))

    def claim_upload_job(self):
        with self.transaction() as db:
            row = db.execute("SELECT id FROM upload_jobs WHERE status='queued' ORDER BY created_at,rowid LIMIT 1").fetchone()
            if row is None:
                return None
            db.execute("UPDATE upload_jobs SET status='processing',stage='reading',error=NULL,updated_at=? WHERE id=?",
                       (current_time(), row["id"]))
            return self.read_upload_job(db, row["id"])

    def update_upload_progress(self, job_id, completed_pages, total_pages, stage):
        completed_pages = max(0, int(completed_pages))
        total_pages=  max(completed_pages, int(total_pages), 0)
        with self.transaction() as db:
            db.execute("UPDATE upload_jobs SET completed_pages=?,total_pages=?,stage=?,updated_at=? WHERE id=? AND status='processing'",
                       (completed_pages, total_pages, str(stage)[:40], current_time(), str(job_id)))

    def fail_upload_job(self, job_id, error):
        with self.transaction() as db:
            db.execute("UPDATE upload_jobs SET status='failed',stage='failed',error=?,updated_at=? WHERE id=? AND status='processing'",
                       (str(error)[:500], current_time(), str(job_id)))

    def prune_upload_jobs(self, keep=100):
        keep = max(1, int(keep))
        with self.transaction() as db:
            db.execute("DELETE FROM upload_jobs WHERE id IN (SELECT id FROM upload_jobs WHERE status IN ('completed','failed') ORDER BY updated_at DESC,rowid DESC LIMIT -1 OFFSET ?)",
                       (keep,))

    def complete_upload_job(self, job_id, text, topic_review=None):
        with self.transaction() as db:
            job = self.read_upload_job(db, job_id)
            if job["status"] == "completed":
                return job
            if job["status"] != "processing":
                raise DomainError("Загрузка учебника сейчас не обрабатывается.")
            title, subject, text = self.validate_book(job["title"], job["subject"], text)
            cursor = db.execute("INSERT INTO books(title,subject,text,created_at) VALUES(?,?,?,?)",
                                (title, subject, text, current_time()))
            review  = topic_review or self.local_topics(text)
            self.save_topics(db, cursor.lastrowid, text, review)
            self.add_audit(db, "admin", "add_book", {"id": cursor.lastrowid, "title": title})
            db.execute("UPDATE upload_jobs SET status='completed',stage='completed',error=NULL,book_id=?,completed_pages=total_pages,updated_at=? WHERE id=?",
                       (cursor.lastrowid, current_time(), str(job_id)))
            return self.read_upload_job(db, job_id)

    @staticmethod
    def read_book(db, book_id):
        row = db.execute("SELECT * FROM books WHERE id=? AND archived=0", (book_id,)).fetchone()
        if row is None:
            raise DomainError("Учебник не найден.")
        return dict(row)

    def get_book(self, book_id):
        with self.transaction() as db:
            return self.read_book(db, book_id)

    def book_topics(self, book_id):
        return self.book_topic_status(book_id)["topics"]

    @staticmethod
    def text_hash(text):
        return hashlib.sha256(text.encode("utf-8")).hexdigest()

    @classmethod
    def local_topics(cls, text):
        from topics import find_topics

        return {"topics": find_topics(text), "source": "local", "status": "unreviewed",
                "error": None, "text_hash": cls.text_hash(text), "revision": 2}

    @classmethod
    def save_topics(cls, db, book_id, text, review):
        if review["text_hash"] != cls.text_hash(text):
            raise DomainError("Текст учебника изменился. Повторите проверку тем.")
        db.execute("""INSERT INTO book_topic_cache(book_id,text_hash,revision,topics,source,status,error,updated_at)
            VALUES(?,?,?,?,?,?,?,?) ON CONFLICT(book_id) DO UPDATE SET
            text_hash=excluded.text_hash,revision=excluded.revision,topics=excluded.topics,
            source=excluded.source,status=excluded.status,error=excluded.error,updated_at=excluded.updated_at""",
            (book_id, review["text_hash"], 2, json_text(review["topics"]), review["source"],
             review["status"], review.get("error"), current_time()))

    def book_topic_status(self, book_id):
        with self.transaction() as db:
            book =  self.read_book(db, book_id)
            row = db.execute("SELECT * FROM book_topic_cache WHERE book_id=?", (book_id,)).fetchone()
            if row is None or row["text_hash"] != self.text_hash(book["text"]) or row["revision"] != 2:
                self.save_topics(db, book_id, book["text"], self.local_topics(book["text"]))
                row = db.execute("SELECT * FROM book_topic_cache WHERE book_id=?", (book_id,)).fetchone()
            result = dict(row)
            result["topics"] = load_json(result["topics"], [])
            return result

    def prepare_book_topics(self, text):
        from generation import GenerationError
        from topics import clean_topics

        text=  str(text).strip()
        self.validate_book("Учебник", "Предмет", text)
        result = self.local_topics(text)
        reviewer = getattr(self.generator, "review_topics", None)
        if not callable(reviewer):
            return result
        try:
            reviewed = reviewer(text)
            if not isinstance(reviewed, list):
                raise ValueError("invalid topic review")
            for topic in reviewed:
                if not isinstance(topic, str):
                    raise ValueError("invalid topic review")
            result["topics"] = clean_topics(reviewed)
            result["source"] = "ai"
            result["status"] = "reviewed"
        except Exception as exc:
            error  = str(exc) if isinstance(exc, (DomainError, GenerationError)) else "Не удалось проверить темы с помощью ИИ."
            result["status"] = "failed"
            result["error"] = error[:500]
        return result

    def refresh_book_topics(self, book_id):
        book = self.get_book(book_id)
        review = self.prepare_book_topics(book["text"])
        with self.transaction() as db:
            current = self.read_book(db, book_id)
            if current["text"] != book["text"]:
                raise DomainError("Текст учебника изменился. Повторите проверку тем.")
            previous =  db.execute("SELECT * FROM book_topic_cache WHERE book_id=?", (book_id,)).fetchone()
            if (review["status"] == "failed" and previous is not None
                    and previous["source"] == "ai" and previous["text_hash"] == review["text_hash"]
                    and previous["revision"] == 2):
                review["topics"] = load_json(previous["topics"], [])
                review["source"] = "ai"
            self.save_topics(db, book_id, book["text"], review)
        return self.book_topic_status(book_id)

    def list_books(self):
        with self.transaction() as db:
            books = []
            for row in db.execute("SELECT id,title,subject,created_at FROM books WHERE archived=0 ORDER BY id DESC"):
                books.append(dict(row))
            return books

    def delete_book(self, book_id, actor="admin"):
        with self.transaction() as db:
            self.check_admin(db, actor)
            self.read_book(db, book_id)
            db.execute("UPDATE books SET archived=1 WHERE id=?", (book_id,))
            settings = self.read_settings(db)
            if settings["mini_book_id"] == int(book_id):
                settings["mini_book_id"] = None
                db.execute("UPDATE metadata SET value=? WHERE key='settings'", (json_text(settings),))
            self.add_audit(db, actor, "delete_book", {"id": book_id})

    @staticmethod
    def validate_questions(questions, count=None):
        if not isinstance(questions, list) or not 1 <= len(questions) <= 30:
            raise DomainError("В работе должно быть от 1 до 30 вопросов.")
        if count is not None and len(questions) != count:
            raise DomainError("ИИ вернул неверное количество вопросов. Попробуйте снова.")
        result = []
        for number, question_data in enumerate(questions, 1):
            if not isinstance(question_data, dict):
                raise DomainError(f"Вопрос {number} имеет неверный формат.")
            options = question_data.get("options")
            answer = question_data.get("answer")
            question = str(question_data.get("question", "")).strip()
            error = f"Вопрос {number}: нужны текст, четыре разных ответа и номер правильного ответа от 0 до 3."
            if not question or not isinstance(options, list) or len(options) != 4:
                raise DomainError(error)
            if type(answer) is not int or not 0 <= answer <= 3:
                raise DomainError(error)
            cleaned = []
            used = []
            for option in options:
                if not isinstance(option, str) or not option.strip():
                    raise DomainError(error)
                option = option.strip()
                if option.casefold() in used:
                    raise DomainError(error)
                used.append(option.casefold())
                cleaned.append(option)
            result.append({"question": question, "options": cleaned,
                           "answer": answer, "explanation": str(question_data.get("explanation") or "").strip()})
        return result

    def generate_questions(self, book, topic, count):
        if self.generator is None:
            raise DomainError("Генератор ИИ не настроен. Добавьте ключ OpenAI API на сервере.")
        return self.validate_questions(self.generator.generate(book["text"], topic, count), count)

    @staticmethod
    def read_quiz(db, quiz_id):
        row=  db.execute("""SELECT q.*,b.title AS book_title,b.subject,g.name AS group_name
            FROM quizzes q JOIN books b ON b.id=q.book_id LEFT JOIN groups g ON g.id=q.group_id
            WHERE q.id=?""", (quiz_id,)).fetchone()
        if row is None:
            raise DomainError("Работа не найдена.")
        result = dict(row)
        result["questions"] = load_json(result["questions"], [])
        result["count"] = len(result["questions"])
        return result

    def create_quiz(self, book_id, topic, kind="assessment", count=10, group_id=None, actor="admin"):
        topic = str(topic).strip()
        if not 3 <= len(topic) <= 250:
            raise DomainError("Укажите тему работы (от 3 до 250 символов).")
        if kind not in {"assessment", "mini", "test"}:
            raise DomainError("Неизвестный тип работы.")
        if type(count) is not int or not 1 <= count <= 30 or (kind in {"mini", "test"} and count != 10):
            raise DomainError("Квиз содержит 1–30 вопросов, тест с монетами — ровно 10.")
        with self.transaction() as db:
            self.check_admin(db, actor)
            group_id = self.read_group(db, group_id)
            book  = self.read_book(db, book_id)
            settings = self.read_settings(db)
            day = self.parse_day(None, settings).isoformat() if kind == "mini" else None
            if kind == "mini" and db.execute("SELECT id FROM quizzes WHERE kind='mini' AND day=? AND group_key=?",
                                            (day, group_id or 0)).fetchone():
                raise DomainError("Мини-тест для этого класса на сегодня уже создан.")
        questions = self.generate_questions(book, topic, count)
        with self.transaction() as db:
            self.check_admin(db, actor)
            self.read_group(db, group_id)
            self.read_book(db, book_id)
            if kind == "mini" and db.execute("SELECT id FROM quizzes WHERE kind='mini' AND day=? AND group_key=?",
                                            (day, group_id or 0)).fetchone():
                raise DomainError("Мини-тест для этого класса на сегодня уже создан.")
            cursor =  db.execute("""INSERT INTO quizzes(book_id,topic,kind,group_id,group_key,questions,day,trimester_start,created_at)
                VALUES(?,?,?,?,?,?,?,?,?)""", (book_id, topic, kind, group_id, group_id or 0, json_text(questions),
                                                day, settings["trimester_start"], current_time()))
            self.add_audit(db, actor, "create_quiz", {"id": cursor.lastrowid, "topic": topic, "kind": kind})
            return self.read_quiz(db, cursor.lastrowid)

    def get_quiz(self, quiz_id):
        with self.transaction() as db:
            return self.read_quiz(db, quiz_id)

    def list_quizzes(self, kind=None, published_only=False, group_id=None):
        with self.transaction() as db:
            clauses, params = ["status!='deleted'"], []
            if kind:
                clauses.append("kind=?")
                params.append(kind)
            if published_only:
                clauses.append("status='published'")
            if group_id is not None:
                clauses.append("(group_id IS NULL OR group_id=?)")
                params.append(group_id)
            query = "SELECT id FROM quizzes WHERE " + " AND ".join(clauses) + " ORDER BY id DESC"
            quizzes = []
            for row in db.execute(query, params):
                quizzes.append(self.read_quiz(db, row["id"]))
            return quizzes

    def available_assessments(self, user_id):
        with self.transaction() as db:
            user_id = self.add_user_if_missing(db, user_id)
            user = self.read_user(db, user_id)
            if not user["registered"]:
                return []
            rows=  db.execute("""SELECT q.id FROM quizzes q WHERE q.kind='assessment' AND q.status='published'
                AND (q.group_id IS NULL OR q.group_id=?) AND NOT EXISTS
                (SELECT 1 FROM attempts a WHERE a.quiz_id=q.id AND a.user_id=? AND a.status='finished') ORDER BY q.id DESC""",
                              (user["group_id"], user_id))
            quizzes = []
            for row in rows:
                quizzes.append(self.read_quiz(db, row["id"]))
            return quizzes

    def available_tests(self, user_id, day=None):
        with self.transaction() as db:
            user_id = self.add_user_if_missing(db, user_id)
            user = self.read_user(db, user_id)
            settings = self.read_settings(db)
            day  = self.parse_day(day, settings)
            if (not user["registered"] or not date.fromisoformat(settings["trimester_start"])
                    <= day <= date.fromisoformat(settings["trimester_end"])):
                return []
            rows = db.execute("""SELECT q.id FROM quizzes q WHERE q.kind='test' AND q.status='published'
                AND (q.group_id IS NULL OR q.group_id=?) AND NOT EXISTS
                (SELECT 1 FROM attempts a WHERE a.quiz_id=q.id AND a.user_id=? AND a.status='finished') ORDER BY q.id DESC""",
                              (user["group_id"], user_id))
            quizzes = []
            for row in rows:
                quizzes.append(self.read_quiz(db, row["id"]))
            return quizzes

    def update_quiz_questions(self, quiz_id, questions, actor="admin"):
        with self.transaction() as db:
            self.check_admin(db, actor)
            quiz = self.read_quiz(db, quiz_id)
            if quiz["status"] != "draft":
                raise DomainError("Можно редактировать только неопубликованный черновик.")
            questions = self.validate_questions(questions, 10 if quiz["kind"] in {"mini", "test"} else None)
            db.execute("UPDATE quizzes SET questions=? WHERE id=?", (json_text(questions), quiz_id))
            self.add_audit(db, actor, "update_quiz_questions", {"id": quiz_id})
            return self.read_quiz(db, quiz_id)

    def publish_quiz(self, quiz_id, actor="admin"):
        with self.transaction() as db:
            self.check_admin(db, actor)
            quiz =  self.read_quiz(db, quiz_id)
            if quiz["status"] in {"closed", "deleted"}:
                raise DomainError("Закрытую или удалённую работу нельзя публиковать повторно.")
            db.execute("UPDATE quizzes SET status='published' WHERE id=?", (quiz_id,))
            self.add_audit(db, actor, "publish_quiz", {"id": quiz_id})
            return self.read_quiz(db, quiz_id)

    def close_quiz(self, quiz_id, actor="admin"):
        with self.transaction() as db:
            self.check_admin(db, actor)
            if self.read_quiz(db, quiz_id)["status"] == "deleted":
                raise DomainError("Работа удалена.")
            db.execute("UPDATE quizzes SET status='closed' WHERE id=?", (quiz_id,))
            self.add_audit(db, actor, "close_quiz", {"id": quiz_id})
            return self.read_quiz(db, quiz_id)

    def delete_quiz(self, quiz_id, actor="admin"):
        with self.transaction() as db:
            self.check_admin(db, actor)
            quiz = self.read_quiz(db, quiz_id)
            if quiz["status"] != "deleted":
                db.execute("UPDATE quizzes SET status='deleted' WHERE id=?", (quiz_id,))
                self.add_audit(db, actor, "delete_quiz", {"id": quiz_id, "kind": quiz["kind"]})
            return self.read_quiz(db, quiz_id)

    def settings(self):
        with self.transaction() as db:
            return self.read_settings(db)

    def update_settings(self, values, actor="admin"):
        if not isinstance(values, dict):
            raise DomainError("Неизвестные настройки.")
        for key in values:
            if key not in DEFAULT_SETTINGS:
                raise DomainError("Неизвестные настройки.")
        with self.transaction() as db:
            self.check_admin(db, actor)
            settings = self.read_settings(db)
            settings.update(values)
            settings["enabled_certificates"] = self.certificate_keys(db, settings["enabled_certificates"])
            start=  self.parse_day(settings["trimester_start"], settings)
            end = self.parse_day(settings["trimester_end"], settings)
            if end < start or (end - start).days > 366:
                raise DomainError("Проверьте даты триместра (не более 366 дней).")
            settings["trimester_start"], settings["trimester_end"] = start.isoformat(), end.isoformat()
            if type(settings["active_week_parity"]) is not int or settings["active_week_parity"] not in (0, 1):
                raise DomainError("Номер чередования недели должен быть 0 или 1.")
            if type(settings["max_active_weeks"]) is not int or not 1 <= settings["max_active_weeks"] <= 5:
                raise DomainError("В триместре может быть от 1 до 5 активных недель.")
            time_zone(settings["timezone"])
            if not isinstance(settings["holidays"], list):
                raise DomainError("Нерабочие дни должны быть списком дат.")
            holidays = []
            for day in settings["holidays"]:
                day = self.parse_day(day, settings).isoformat()
                if day not in holidays:
                    holidays.append(day)
            settings["holidays"] = sorted(holidays)
            if settings["mini_book_id"] in (None, ""):
                settings["mini_book_id"] = None
            else:
                self.read_book(db, settings["mini_book_id"])
                settings["mini_book_id"] = int(settings["mini_book_id"])
            settings["mini_topic"] = str(settings["mini_topic"]).strip()
            if settings["mini_book_id"] is not None and (
                    not 3 <= len(settings["mini_topic"]) <= 250
                    or settings["mini_topic"] == "Повторение изученного материала"):
                raise DomainError("Выберите конкретную тему из учебника для ежедневных мини-тестов (от 3 до 250 символов).")
            if len(settings["mini_topic"]) > 250:
                raise DomainError("Тема ежедневных мини-тестов должна быть не длиннее 250 символов.")
            db.execute("UPDATE metadata SET value=? WHERE key='settings'", (json_text(settings),))
            changed = {}
            for key in values:
                changed[key] = settings[key]
            self.add_audit(db, actor, "update_settings", changed)
            return settings

    def available_mini(self, day=None, group_id=None):
        owner = uuid.uuid4().hex
        with self.transaction() as db:
            settings = self.read_settings(db)
            day  = self.parse_day(day, settings)
            if not self.eligible_day(day, settings):
                return None
            if not settings["mini_book_id"] or not settings["mini_topic"] or settings["mini_topic"] == "Повторение изученного материала":
                return None
            group_id = self.read_group(db, group_id)
            existing = db.execute("SELECT id,status FROM quizzes WHERE kind='mini' AND day=? AND group_key=?",
                                  (day.isoformat(), group_id or 0)).fetchone()
            if existing:
                return self.read_quiz(db, existing["id"]) if existing["status"] == "published" else None
            book = self.read_book(db, settings["mini_book_id"])
            job_key =  f"mini:{day.isoformat()}:{group_id or 0}"
            job = db.execute("SELECT owner,started_at FROM generation_jobs WHERE job_key=?", (job_key,)).fetchone()
            if job and job["started_at"] > time.time() - 600:
                raise DomainError("Мини-тест уже готовится. Повторите запрос через несколько минут.")
            db.execute("INSERT INTO generation_jobs(job_key,owner,started_at) VALUES(?,?,?) "
                       "ON CONFLICT(job_key) DO UPDATE SET owner=excluded.owner,started_at=excluded.started_at",
                       (job_key, owner, time.time()))
        try:
            topic = settings["mini_topic"]
            questions = self.generate_questions(book, f"{topic} [{day.isoformat()}]", 10)
            with self.transaction() as db:
                job=  db.execute("SELECT owner FROM generation_jobs WHERE job_key=?", (job_key,)).fetchone()
                if not job or job["owner"] != owner:
                    raise DomainError("Подготовка мини-теста была перезапущена. Повторите запрос.")
                current = self.read_settings(db)
                changed = not self.eligible_day(day, current)
                for key in ("mini_book_id", "mini_topic", "trimester_start", "timezone"):
                    if current[key] != settings[key]:
                        changed = True
                if changed:
                    raise DomainError("Расписание или источник заданий изменились. Повторите подготовку мини-теста.")
                self.read_book(db, book["id"])
                self.read_group(db, group_id)
                existing = db.execute("SELECT id,status FROM quizzes WHERE kind='mini' AND day=? AND group_key=?",
                                      (day.isoformat(), group_id or 0)).fetchone()
                if existing:
                    return self.read_quiz(db, existing["id"]) if existing["status"] == "published" else None
                cursor = db.execute("""INSERT INTO quizzes(book_id,topic,kind,group_id,group_key,status,questions,day,trimester_start,created_at)
                    VALUES(?,?,'mini',?,?,'published',?,?,?,?)""", (book["id"], topic, group_id, group_id or 0,
                        json_text(questions), day.isoformat(), settings["trimester_start"], current_time()))
                self.add_audit(db, "scheduler", "generate_mini", {"id": cursor.lastrowid, "day": day.isoformat(), "group_id": group_id})
                return self.read_quiz(db, cursor.lastrowid)
        finally:
            with self.transaction() as db:
                db.execute("DELETE FROM generation_jobs WHERE job_key=? AND owner=?", (job_key, owner))

    @staticmethod
    def read_attempt(db, attempt_id, user_id=None):
        row  = db.execute("SELECT * FROM attempts WHERE id=?", (attempt_id,)).fetchone()
        if row is None or (user_id is not None and row["user_id"] != str(user_id)):
            raise DomainError("Попытка не найдена.")
        result = dict(row)
        result["index"] = result.pop("question_index")
        result["answers"] = load_json(result["answers"], [])
        return result

    def attempt_data(self, db, attempt):
        quiz = self.read_quiz(db, attempt["quiz_id"])
        return {"id": attempt["id"], "attempt_id": attempt["id"], "quiz_id": attempt["quiz_id"],
                "user_id": attempt["user_id"], "kind": attempt["kind"], "index": attempt["index"],
                "score": attempt["score"], "coins": attempt["coins"], "status": attempt["status"],
                "start_day": attempt["start_day"], "total": quiz["count"], "topic": quiz["topic"]}

    def active_attempt(self, user_id):
        with self.transaction() as db:
            row = db.execute("SELECT id FROM attempts WHERE user_id=? AND status='active'", (str(user_id),)).fetchone()
            if row:
                attempt = self.read_attempt(db, row["id"])
                return self.attempt_data(db, attempt)
            return None

    def start_attempt(self, user_id, quiz_id, day=None):
        with self.transaction() as db:
            user_id =  self.add_user_if_missing(db, user_id)
            user = self.read_user(db, user_id)
            if not user["registered"]:
                raise DomainError("Сначала зарегистрируйтесь: укажите ФИО и класс.")
            active = db.execute("SELECT id,quiz_id FROM attempts WHERE user_id=? AND status='active'", (user_id,)).fetchone()
            if active:
                if active["quiz_id"] == int(quiz_id):
                    return self.attempt_data(db, self.read_attempt(db, active["id"]))
                raise DomainError("Сначала завершите уже начатую работу.")
            quiz = self.read_quiz(db, quiz_id)
            if quiz["status"] != "published":
                raise DomainError("Эта работа сейчас недоступна.")
            if quiz["group_id"] is not None and quiz["group_id"] != user["group_id"]:
                raise DomainError("Эта работа назначена другому классу.")
            if db.execute("SELECT id FROM attempts WHERE user_id=? AND quiz_id=?", (user_id, quiz_id)).fetchone():
                raise DomainError("Вы уже выполнили эту работу.")
            settings=  self.read_settings(db)
            day = self.parse_day(day, settings)
            if quiz["kind"] == "test" and not (date.fromisoformat(settings["trimester_start"])
                    <= day <= date.fromisoformat(settings["trimester_end"])):
                raise DomainError("Тесты с монетами доступны в действующем триместре.")
            if quiz["kind"] == "mini":
                if (not settings["mini_book_id"] or not settings["mini_topic"]
                        or settings["mini_topic"] == "Повторение изученного материала"
                        or not self.eligible_day(day, settings) or quiz["day"] != day.isoformat()
                        or quiz["trimester_start"] != settings["trimester_start"]):
                    raise DomainError("Этот мини-тест недоступен сегодня.")
                if (db.execute("SELECT 1 FROM attempts WHERE user_id=? AND kind='mini' AND start_day=?", (user_id, day.isoformat())).fetchone()
                        or db.execute("SELECT 1 FROM mini_completions WHERE user_id=? AND day=?", (user_id, day.isoformat())).fetchone()):
                    raise DomainError("Сегодняшний мини-тест уже пройден.")
            week_start = (day - timedelta(days=day.weekday())).isoformat()
            cursor = db.execute("""INSERT INTO attempts(user_id,quiz_id,kind,group_id,start_day,trimester_start,week_start,created_at)
                VALUES(?,?,?,?,?,?,?,?)""", (user_id, quiz_id, quiz["kind"], user["group_id"], day.isoformat(),
                                             settings["trimester_start"], week_start, current_time()))
            return self.attempt_data(db, self.read_attempt(db, cursor.lastrowid))

    def question(self, attempt_id, user_id):
        with self.transaction() as db:
            attempt  = self.read_attempt(db, attempt_id, user_id)
            if attempt["status"] != "active":
                raise DomainError("Эта работа уже завершена.")
            quiz = self.read_quiz(db, attempt["quiz_id"])
            question = quiz["questions"][attempt["index"]]
            return {"attempt_id": attempt["id"], "index": attempt["index"], "total": quiz["count"],
                    "question": question["question"], "options": question["options"], "topic": quiz["topic"]}

    def answer(self, attempt_id, user_id, index, option, day=None):
        if type(index) is not int or type(option) is not int or not 0 <= option <= 3:
            raise DomainError("Выберите один из четырёх ответов.")
        with self.transaction() as db:
            attempt = self.read_attempt(db, attempt_id, user_id)
            if not self.read_user(db, user_id)["registered"]:
                raise DomainError("Сначала зарегистрируйтесь: укажите ФИО и класс.")
            if attempt["status"] != "active" or index != attempt["index"]:
                raise DomainError("Этот ответ уже обработан. Откройте текущий вопрос.")
            quiz =  self.read_quiz(db, attempt["quiz_id"])
            question = quiz["questions"][index]
            correct = option == question["answer"]
            score = attempt["score"] + int(correct)
            answers=  attempt["answers"] + [option]
            done = index + 1 == quiz["count"]
            coins = 0
            if done and attempt["kind"] in {"mini", "test"}:
                daily = db.execute("SELECT COALESCE(SUM(amount),0) FROM coin_awards WHERE user_id=? AND day=?",
                                   (str(user_id), attempt["start_day"])).fetchone()[0]
                weekly  = db.execute("SELECT COALESCE(SUM(amount),0) FROM coin_awards WHERE user_id=? AND week_start=?",
                                    (str(user_id), attempt["week_start"])).fetchone()[0]
                term = db.execute("SELECT COALESCE(SUM(amount),0) FROM coin_awards WHERE user_id=? AND trimester_start=?",
                                  (str(user_id), attempt["trimester_start"])).fetchone()[0]
                coins = max(0, min(score, 10 - daily, 50 - weekly, 250 - term))
                db.execute("INSERT INTO coin_awards(user_id,attempt_id,day,week_start,trimester_start,amount) VALUES(?,?,?,?,?,?)",
                           (str(user_id), attempt_id, attempt["start_day"], attempt["week_start"], attempt["trimester_start"], coins))
                db.execute("UPDATE users SET coins=coins+? WHERE id=?", (coins, str(user_id)))
                if attempt["kind"] == "mini":
                    db.execute("INSERT OR IGNORE INTO mini_completions(user_id,day) VALUES(?,?)", (str(user_id), attempt["start_day"]))
            db.execute("UPDATE attempts SET question_index=?,score=?,coins=?,answers=?,status=?,finished_at=? WHERE id=?",
                       (index + 1, score, coins, json_text(answers), "finished" if done else "active", current_time() if done else None, attempt_id))
            return {"done": done, "score": score, "total": quiz["count"], "correct": correct,
                    "correct_answer": question["options"][question["answer"]], "correct_index": question["answer"],
                    "explanation": question["explanation"], "coins": coins, "balance": self.read_user(db, user_id)["coins"],
                    "index": index + 1, "attempt_id": attempt_id}

    def results(self):
        with self.transaction() as db:
            result = []
            for row in db.execute("""SELECT a.*,u.name AS user_name,g.name AS group_name FROM attempts a
                JOIN users u ON u.id=a.user_id LEFT JOIN groups g ON g.id=a.group_id
                WHERE a.status='finished' ORDER BY a.id DESC"""):
                a =  dict(row)
                quiz = self.read_quiz(db, a["quiz_id"])
                answers = load_json(a["answers"], [])
                errors = []
                for index, question in enumerate(quiz["questions"]):
                    if answers[index] == question["answer"]:
                        continue
                    errors.append({"index": index, "question": question["question"],
                                   "selected": question["options"][answers[index]],
                                   "correct_answer": question["options"][question["answer"]],
                                   "explanation": question["explanation"]})
                result.append({"id": a["id"], "attempt_id": a["id"], "user_id": a["user_id"],
                               "user_name": a["user_name"] or a["user_id"], "group_id": a["group_id"], "group_name": a["group_name"],
                               "quiz_id": a["quiz_id"], "topic": quiz["topic"], "subject": quiz["subject"], "kind": a["kind"],
                               "score": a["score"], "total": quiz["count"], "coins": a["coins"], "date": a["start_day"],
                               "finished_at": a["finished_at"], "errors": errors})
            return result

    @staticmethod
    def certificate_keys(db, keys):
        if not isinstance(keys, list):
            raise DomainError("Выберите сертификаты из списка магазина.")
        catalog = []
        for row in db.execute("SELECT key FROM certificates WHERE archived=0 ORDER BY rowid"):
            catalog.append(row["key"])
        for key in keys:
            if not isinstance(key, str) or key not in catalog:
                raise DomainError("Выберите сертификаты из списка магазина.")
        selected = []
        for key in catalog:
            if key in keys:
                selected.append(key)
        return selected

    def add_certificate(self, title, price, stock, description="", actor="admin"):
        title = " ".join(str(title).split())
        description = str(description).strip()
        if not title or len(title) > 250 or len(description) > 1000:
            raise DomainError("Укажите название до 250 символов и описание до 1000 символов.")
        if type(price) is not int or not 1 <= price <= 1000000:
            raise DomainError("Цена должна быть целым числом от 1 до 1000000 монет.")
        if type(stock) is not int or not 1 <= stock <= 100:
            raise DomainError("Количество на ученика должно быть от 1 до 100 за триместр.")
        with self.transaction() as db:
            self.check_admin(db, actor)
            for row in db.execute("SELECT title FROM certificates WHERE archived=0"):
                if row["title"].casefold() == title.casefold():
                    raise DomainError("Сертификат с таким названием уже существует.")
            key  = "custom_" + uuid.uuid4().hex
            db.execute("INSERT INTO certificates(key,title,price,stock,description,created_at) VALUES(?,?,?,?,?,?)",
                       (key, title, price, stock, description, current_time()))
            settings = self.read_settings(db)
            settings["enabled_certificates"] = self.certificate_keys(db, settings["enabled_certificates"] + [key])
            db.execute("UPDATE metadata SET value=? WHERE key='settings'", (json_text(settings),))
            self.add_audit(db, actor, "add_certificate", {"key": key, "title": title, "price": price, "stock": stock})
            return {"key": key, "title": title, "price": price, "stock": stock,
                    "description": description, "enabled": True}

    def delete_certificate(self, key, actor="admin"):
        with self.transaction() as db:
            self.check_admin(db, actor)
            item = db.execute("SELECT * FROM certificates WHERE key=?", (key,)).fetchone()
            if item is None:
                raise DomainError("Такого сертификата нет.")
            if not item["archived"]:
                db.execute("UPDATE certificates SET archived=1 WHERE key=?", (key,))
                settings = self.read_settings(db)
                if key in settings["enabled_certificates"]:
                    settings["enabled_certificates"].remove(key)
                db.execute("UPDATE metadata SET value=? WHERE key='settings'", (json_text(settings),))
                self.add_audit(db, actor, "delete_certificate", {"key": key, "title": item["title"]})
            result = dict(item)
            result["archived"] = 1
            result["enabled"] = False
            return result

    def shop_catalog(self):
        with self.transaction() as db:
            settings =  self.read_settings(db)
            catalog = []
            for row in db.execute("SELECT key,title,price,stock,description FROM certificates WHERE archived=0 ORDER BY rowid"):
                item = dict(row)
                item["enabled"] = item["key"] in settings["enabled_certificates"]
                catalog.append(item)
            return catalog

    def set_enabled_certificates(self, keys, actor="admin"):
        self.update_settings({"enabled_certificates": keys}, actor=actor)
        return self.shop_catalog()

    def shop(self, user_id=None):
        with self.transaction() as db:
            settings = self.read_settings(db)
            consumed = {}
            if user_id is not None:
                for row in db.execute("""SELECT item_key,COUNT(*) AS used FROM requests WHERE user_id=?
                    AND trimester_start=? AND status!='rejected' GROUP BY item_key""", (str(user_id), settings["trimester_start"])):
                    consumed[row["item_key"]] = row["used"]
            catalog = []
            for row in db.execute("SELECT key,title,price,stock,description FROM certificates WHERE archived=0 ORDER BY rowid"):
                item = dict(row)
                if item["key"] in settings["enabled_certificates"]:
                    item["remaining"] = max(0, item["stock"] - consumed.get(item["key"], 0))
                    catalog.append(item)
            return catalog

    @staticmethod
    def read_request(db, request_id):
        row = db.execute("SELECT r.*,u.name AS user_name FROM requests r JOIN users u ON u.id=r.user_id WHERE r.id=?",
                         (request_id,)).fetchone()
        if not row:
            raise DomainError("Заявка не найдена.")
        result=  dict(row)
        result["title"] = result["item"]
        return result

    def buy(self, user_id, key, idempotency_key=None):
        if not isinstance(key, str) or not key or len(key) > 100:
            raise DomainError("Такого сертификата нет.")
        if idempotency_key is not None and (not isinstance(idempotency_key, str) or not idempotency_key or len(idempotency_key) > 2000):
            raise DomainError("Некорректный идентификатор покупки.")
        with self.transaction() as db:
            user_id = self.add_user_if_missing(db, user_id)
            if idempotency_key is not None:
                previous = db.execute("SELECT * FROM purchase_callbacks WHERE callback_id=?", (idempotency_key,)).fetchone()
                if previous:
                    if previous["user_id"] != user_id or previous["item_key"] != key:
                        raise DomainError("Идентификатор покупки уже использован.")
                    return self.read_request(db, previous["request_id"])
            user = self.read_user(db, user_id)
            if not user["registered"]:
                raise DomainError("Сначала зарегистрируйтесь: укажите ФИО и класс.")
            item  = db.execute("SELECT * FROM certificates WHERE key=?", (key,)).fetchone()
            if item is None or item["archived"]:
                raise DomainError("Такого сертификата нет.")
            settings = self.read_settings(db)
            if key not in settings["enabled_certificates"]:
                raise DomainError("Этот сертификат сейчас недоступен в магазине.")
            today = self.parse_day(None, settings)
            if not date.fromisoformat(settings["trimester_start"]) <= today <= date.fromisoformat(settings["trimester_end"]):
                raise DomainError("Магазин откроется в действующем триместре.")
            used = db.execute("SELECT COUNT(*) FROM requests WHERE user_id=? AND item_key=? AND trimester_start=? AND status!='rejected'",
                              (user_id, key, settings["trimester_start"])).fetchone()[0]
            if used >= item["stock"]:
                raise DomainError("В этом триместре эти сертификаты закончились.")
            if user["coins"] < item["price"]:
                raise DomainError(f"Недостаточно монет: нужно {item['price']}, на балансе {user['coins']}.")
            db.execute("UPDATE users SET coins=coins-? WHERE id=?", (item["price"], user_id))
            cursor =  db.execute("INSERT INTO requests(user_id,item_key,item,price,trimester_start,created_at) VALUES(?,?,?,?,?,?)",
                                (user_id, key, item["title"], item["price"], settings["trimester_start"], current_time()))
            if idempotency_key is not None:
                db.execute("INSERT INTO purchase_callbacks(callback_id,user_id,item_key,request_id) VALUES(?,?,?,?)",
                           (idempotency_key, user_id, key, cursor.lastrowid))
            self.add_audit(db, user_id, "buy", {"id": cursor.lastrowid, "item_key": key, "price": item["price"]})
            return self.read_request(db, cursor.lastrowid)

    def list_requests(self, user_id=None):
        with self.transaction() as db:
            query = "SELECT id FROM requests"
            params = ()
            if user_id is not None:
                query += " WHERE user_id=?"
                params = (str(user_id),)
            requests = []
            for row in db.execute(query + " ORDER BY id DESC", params):
                requests.append(self.read_request(db, row["id"]))
            return requests

    def resolve_request(self, request_id, action, actor="admin"):
        if action not in {"approve", "reject", "redeem"}:
            raise DomainError("Неизвестное действие с заявкой.")
        with self.transaction() as db:
            self.check_admin(db, actor)
            request=  self.read_request(db, request_id)
            status = request["status"]
            target = {"approve": "approved", "reject": "rejected", "redeem": "redeemed"}[action]
            if status == target:
                return request
            if action == "approve" and status != "pending":
                raise DomainError("Подтвердить можно только ожидающую заявку.")
            if action == "redeem" and status != "approved":
                raise DomainError("Использовать можно только подтверждённый сертификат.")
            if action == "reject" and status not in {"pending", "approved"}:
                raise DomainError("Эту заявку уже нельзя отклонить.")
            if action == "reject":
                db.execute("UPDATE users SET coins=coins+? WHERE id=?", (request["price"], request["user_id"]))
            db.execute("UPDATE requests SET status=?,resolved_at=?,resolved_by=? WHERE id=?", (target, current_time(), str(actor), request_id))
            self.add_audit(db, actor, "resolve_request", {"id": request_id, "action": action, "before": status})
            return self.read_request(db, request_id)

    def audit(self):
        with self.transaction() as db:
            records = []
            for row in db.execute("SELECT * FROM audit_log ORDER BY id DESC"):
                record = dict(row)
                record["details"] = load_json(record["details"], {})
                records.append(record)
            return records

    def get_meta(self, key, default=None):
        with self.transaction() as db:
            row = db.execute("SELECT value FROM metadata WHERE key=?", (str(key),)).fetchone()
            return load_json(row["value"]) if row else default

    def set_meta(self, key, value):
        if key == "settings":
            raise DomainError("Для настроек используйте update_settings.")
        with self.transaction() as db:
            db.execute("INSERT INTO metadata(key,value) VALUES(?,?) ON CONFLICT(key) DO UPDATE SET value=excluded.value",
                       (str(key), json_text(value)))

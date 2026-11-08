from pathlib import Path
import os
import shutil
import threading
import uuid

from study import DomainError

registry_lock = threading.Lock()
worker_locks = {}
submit_lock = threading.Lock()

class Interrupted(Exception):
    pass

class IngestionQueue:
    def setup(self, service, data_dir, extractor=None):
        self.service = service
        self.upload_dir = Path(data_dir) / "uploads"
        self.upload_dir.mkdir(parents=True, exist_ok=True)
        self.extractor = extractor
        if service.db_path == ":memory:":
            key = id(service)
        else:
            key = str(Path(service.db_path).resolve())
        with registry_lock:
            if key not in worker_locks:
                worker_locks[key] = threading.Lock()
            self.worker_lock = worker_locks[key]
        return self

    def check_filename(self, filename):
        filename = str(filename or "").replace("\\", "/").rsplit("/", 1)[-1]
        clean = ""
        for letter in filename:
            if letter.isprintable():
                clean += letter
        filename = clean.strip()
        if not filename or len(filename) > 200:
            raise DomainError("Название файла отсутствует или слишком длинное (максимум 200 символов).")
        if Path(filename).suffix.lower() not in (".txt", ".pdf", ".docx", ".png", ".jpg", ".jpeg", ".webp", ".tif", ".tiff"):
            raise DomainError("Поддерживаются TXT, PDF, DOCX и изображения PNG, JPG, WEBP, TIFF.")
        return filename

    def submit(self, fileobj, filename, title, subject, force_ocr=False):
        import generation

        filename = self.check_filename(filename)
        title, subject, _ = self.service.validate_book(title, subject, "pending")
        with submit_lock:
            if self.service.count_pending_upload_jobs() >= 5:
                raise DomainError("В очереди уже 5 учебников. Дождитесь завершения обработки.")
            job_id = uuid.uuid4().hex
            path = self.upload_dir / job_id
            try:
                size = 0
                with path.open("xb") as output:
                    while True:
                        chunk = fileobj.read(min(1024 * 1024, generation.MAX_FILE_BYTES - size + 1))
                        if not chunk:
                            break
                        if not isinstance(chunk, bytes):
                            raise DomainError("Не удалось прочитать загруженный файл.")
                        size += len(chunk)
                        if size > generation.MAX_FILE_BYTES:
                            raise DomainError(f"Максимальный размер учебника — {generation.MAX_UPLOAD_MB} МБ.")
                        output.write(chunk)
                    if size == 0:
                        raise DomainError("Загружен пустой файл.")
                    output.flush()
                    os.fsync(output.fileno())
                return self.service.create_upload_job(job_id, title, subject, filename, force_ocr, 5)
            except Exception:
                path.unlink(missing_ok=True)
                raise

    def get(self, job_id):
        return self.add_retry_flag(self.service.get_upload_job(job_id))

    def list_jobs(self, limit=20):
        jobs = self.service.list_upload_jobs(limit)
        result = []
        for job in jobs:
            result.append(self.add_retry_flag(job))
        return result

    def add_retry_flag(self, job):
        job["can_retry"] = False
        if job["status"] == "failed":
            try:
                with self.upload_path(job["id"]).open("rb") as source:
                    job["can_retry"] = bool(source.read(1))
            except OSError:
                pass
        return job

    def retry(self, job_id):
        with submit_lock:
            job = self.service.get_upload_job(job_id)
            if job["status"] != "failed":
                raise DomainError("Повторить можно только неудачную загрузку.")
            try:
                with self.upload_path(job_id).open("rb") as source:
                    if not source.read(1):
                        raise OSError("empty source")
            except OSError:
                raise DomainError("Исходный файл больше не сохранён. Загрузите учебник заново.") from None
            return self.service.retry_upload_job(job_id, 5)

    def upload_path(self, job_id):
        if len(job_id) != 32:
            raise DomainError("Некорректный идентификатор загрузки.")
        for letter in job_id:
            if letter not in "0123456789abcdef":
                raise DomainError("Некорректный идентификатор загрузки.")
        return self.upload_dir / job_id

    def clean_finished_files(self):
        failed = self.service.failed_upload_ids(100)
        retained = []
        for job_id in failed:
            if self.upload_path(job_id).is_file():
                retained.append(job_id)
            if len(retained) == 5:
                break
        for path in self.upload_dir.iterdir():
            job_id = path.name.removesuffix(".pages")
            valid = len(job_id) == 32
            for letter in job_id:
                if letter not in "0123456789abcdef":
                    valid = False
            if not valid:
                continue
            try:
                job = self.service.get_upload_job(job_id)
            except DomainError:
                self.remove_files(job_id)
            else:
                if job["status"] == "completed" or (job["status"] == "failed" and job_id not in retained):
                    self.remove_files(job_id)

    def remove_files(self, job_id):
        self.upload_path(job_id).unlink(missing_ok=True)
        cache = self.upload_dir / (job_id + ".pages")
        if cache.is_symlink():
            cache.unlink()
        elif cache.is_dir() and cache.resolve().parent == self.upload_dir.resolve():
            shutil.rmtree(cache)

    def process_upload(self, job, stop_event):
        from generation import GenerationError, extract_text

        path = self.upload_path(job["id"])

        def progress(done, total, stage):
            if stop_event.is_set():
                raise Interrupted()
            self.service.update_upload_progress(job["id"], done, total, stage)

        try:
            if stop_event.is_set():
                raise Interrupted()
            content = path.read_bytes()
            extractor = self.extractor or extract_text
            language = "rus+eng"
            if "англ" in job["subject"].lower() or "english" in job["subject"].lower() or "spotlight" in job["filename"].lower():
                language = "eng+rus"
            result = extractor(content, job["filename"], on_progress=progress, force_ocr=bool(job["force_ocr"]),
                               languages=language,
                               page_cache_dir=self.upload_dir / (job["id"] + ".pages"))
            if stop_event.is_set():
                raise Interrupted()
            current = self.service.get_upload_job(job["id"])
            progress(current["completed_pages"], current["total_pages"], "checking_topics")
            topic_review = self.service.prepare_book_topics(result)
            if stop_event.is_set():
                raise Interrupted()
            self.service.complete_upload_job(job["id"], result, topic_review)
        except Exception as error:
            if isinstance(error, Interrupted):
                return
            message = "Не удалось обработать учебник. Проверьте файл и попробуйте загрузить его снова."
            if isinstance(error, (DomainError, GenerationError)):
                message = str(error)
            self.service.fail_upload_job(job["id"], message)
        with submit_lock:
            self.clean_finished_files()
            self.service.prune_upload_jobs()

    def run(self, stop_event):
        if not self.worker_lock.acquire(blocking=False):
            raise RuntimeError("Обработчик загрузок для этой базы уже запущен.")
        try:
            with submit_lock:
                self.clean_finished_files()
                self.service.prune_upload_jobs()
                self.service.recover_upload_jobs()
            while not stop_event.is_set():
                job = self.service.claim_upload_job()
                if job is None:
                    stop_event.wait(0.25)
                else:
                    self.process_upload(job, stop_event)
        finally:
            self.worker_lock.release()

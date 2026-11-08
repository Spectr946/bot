from contextlib import asynccontextmanager
import logging
import os
from pathlib import Path
import threading
import uvicorn
from admin import create_app
from bot import MaxBot, UpdateInbox, poll
from generation import Generator
from ingestion import IngestionQueue
from study import StudyService

def build_app():
    folder = Path(os.environ.get("DATA_DIR", "data"))
    try:
        service = StudyService().setup(folder / "study.sqlite3", generator=Generator().setup())
        uploads = IngestionQueue().setup(service, folder)
        inbox = UpdateInbox().setup(service.db_path)
    except Exception:
        raise RuntimeError("Не удалось открыть файлы бота и базу данных. Проверьте DATA_DIR, права на запись и свободное место.") from None
    app = create_app(service, ingestion=uploads)
    bot = MaxBot().setup(service)
    stop = threading.Event()

    @asynccontextmanager
    async def lifespan(app_instance):
        workers = []
        try:
            if not bot.client.token:
                raise RuntimeError("Задайте MAX_BOT_TOKEN в .env")
            bot.client.api("me")
            workers = [
                threading.Thread(target=poll, args=(bot, inbox, stop), daemon=True),
                threading.Thread(target=inbox.drain, args=(bot, stop), daemon=True),
                threading.Thread(target=uploads.run, args=(stop,), daemon=True),
            ]
            for worker in workers:
                worker.start()
            yield
        finally:
            stop.set()
            running = False
            for worker in workers:
                if worker.ident is not None:
                    worker.join(65)
                if worker.is_alive():
                    running = True
            if not running:
                service.close()
    app.router.lifespan_context = lifespan
    app.state.service = service
    app.state.bot = bot
    app.state.ingestion = uploads
    return app

if __name__ == "__main__":
    logging.basicConfig(level=logging.INFO)
    uvicorn.run(build_app(), host="0.0.0.0", port=8080, proxy_headers=False, access_log=False)

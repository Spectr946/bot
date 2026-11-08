import io
import hashlib
import json
from pathlib import Path
import shutil
import tempfile
import time
import threading
import warnings
from concurrent.futures import ThreadPoolExecutor
from PIL import Image, ImageOps
import pypdfium2
from openai_client import OpenAIClient, OpenAIError
pdf_lock = threading.RLock()

class OCRFailure(ValueError):
    pass

def clean_text(text):
    text = text.replace('\r\n', '\n').replace('\r', '\n').replace('\x0c', '\n\n').replace('\x00', '')
    lines = []
    for line in text.split('\n'):
        line = ' '.join(line.split())
        if line or not lines or lines[-1]:
            lines.append(line)
    return '\n'.join(lines).strip()

def text_too_large(limit):
    number = f'{limit:,}'.replace(',', ' ')
    return OCRFailure(f'Слишком большой учебник: разделите его на части до {number} символов.')

class TextBuffer:

    def setup(self, limit):
        if type(limit) is not int or not 1 <= limit <= 600000:
            raise OCRFailure('Некорректный предел текста: допустимо от 1 до 600 000 символов.')
        self.limit = limit
        self.parts = []
        self.length = 0
        return self

    def add(self, text):
        text = clean_text(text)
        if not text:
            return
        size = len(text)
        if self.parts:
            size += 2
        if self.length + size > self.limit:
            raise text_too_large(self.limit)
        self.parts.append(text)
        self.length += size

    def result(self):
        if not self.parts:
            raise OCRFailure('В документе не найден текст. Проверьте качество скана и загрузите читаемые страницы.')
        return '\n\n'.join(self.parts)

def remaining_time(deadline):
    remaining = deadline - time.monotonic()
    if remaining <= 0:
        raise OCRFailure('Превышено время обработки учебника. Разделите его на несколько файлов.')
    return min(60, remaining)

def prepare_frame(source, target):
    if source.width <= 0 or source.height <= 0 or source.width * source.height > 40000000:
        raise OCRFailure('Изображение слишком большое: максимум 40 миллионов пикселей на страницу.')
    if getattr(source, 'format', None) == 'JPEG':
        source.draft('L', (2400, 2400))
    with source.copy() as frame:
        frame.thumbnail((2400, 2400), Image.Resampling.LANCZOS)
        ImageOps.exif_transpose(frame, in_place=True)
        if 'A' in frame.getbands() or 'transparency' in frame.info:
            with frame.convert('RGBA') as rgba:
                with Image.new('RGBA', frame.size, 'white') as background:
                    background.alpha_composite(rgba)
                    gray = background.convert('L')
        else:
            gray = frame.convert('L')
        with gray:
            with gray.copy() as sample:
                sample.thumbnail((256, 256))
                histogram = sample.histogram()
                pixels = sample.width * sample.height
                cumulative = 0
                background = 255
                for shade, count in enumerate(histogram):
                    cumulative += count
                    if cumulative >= pixels * 0.9:
                        background = shade
                        break
                has_ink = sum(histogram[:max(0, background - 8)]) > pixels * 0.0002
            with ImageOps.autocontrast(gray, cutoff=1) as enhanced:
                enhanced.save(target, format='PNG', optimize=False)
    return has_ink

def recognize(raster, has_ink, client, deadline):
    if not has_ink:
        return ''
    if raster.stat().st_size > 8 * 1024 * 1024:
        raise OCRFailure('Страница OCR слишком большая. Уменьшите размер скана.')
    with Image.open(raster) as image:
        width, height = image.size
    try:
        text = client.transcribe_image(raster.read_bytes(), width=width, height=height, timeout=remaining_time(deadline))
    except Exception as error:
        if isinstance(error, OpenAIError):
            raise OCRFailure(str(error)) from None
        raise OCRFailure('Не удалось распознать страницу. Повторите обработку.') from None
    remaining_time(deadline)
    text = clean_text(text)
    if not text:
        raise OCRFailure('Не удалось распознать текст на непустой странице. Улучшите качество скана или загрузите страницу отдельно.')
    return text

def check_languages(value):
    if value not in {'auto', 'rus+eng', 'eng+rus', 'eng'}:
        raise OCRFailure('Для OCR доступны русский и английский языки.')
    if value == 'auto':
        return 'rus+eng'
    return value

def cache_folder(path, content, options):
    if path is None:
        return None
    source_hash = hashlib.sha256(content).hexdigest()
    metadata = dict(options, source_sha256=source_hash, version=3)
    encoded = json.dumps(metadata, sort_keys=True).encode('utf-8')
    folder = Path(path) / hashlib.sha256(encoded).hexdigest()
    try:
        folder.mkdir(parents=True, exist_ok=True)
        (folder / 'document.json').write_bytes(encoded)
    except Exception:
        raise OCRFailure('Не удалось сохранить прогресс OCR. Проверьте свободное место на сервере.') from None
    return folder

def cached_page(folder, index, limit):
    if folder is None:
        return None
    try:
        path = folder / f'{index + 1:04d}.json'
        if path.stat().st_size > limit * 12 + 100:
            return None
        saved = json.loads(path.read_text(encoding='utf-8'))
        if isinstance(saved, dict) and isinstance(saved.get('text'), str) and (type(saved.get('ocr')) is bool):
            return saved
    except Exception:
        return None
    return None

def save_page(folder, index, text, recognized):
    if folder is None:
        return
    path = folder / f'{index + 1:04d}.json'
    temporary = path.with_suffix('.tmp')
    try:
        temporary.write_text(json.dumps({'text': text, 'ocr': recognized}, ensure_ascii=False), encoding='utf-8')
        temporary.replace(path)
    except Exception:
        raise OCRFailure('Не удалось сохранить прогресс OCR. Проверьте свободное место на сервере.') from None

class PdfDocument:

    def setup(self, content):
        try:
            with pdf_lock:
                self.pdfium = pypdfium2.PdfDocument(content)
        except Exception:
            raise OCRFailure('Не удалось прочитать PDF. Проверьте файл или пароль документа.') from None
        return self

    def close(self):
        with pdf_lock:
            self.pdfium.close()

    def page_count(self):
        with pdf_lock:
            return len(self.pdfium)

    def text(self, index):
        with pdf_lock:
            page = self.pdfium[index]
            try:
                textpage = page.get_textpage()
                try:
                    return textpage.get_text_bounded()
                finally:
                    textpage.close()
            finally:
                page.close()

    def render(self, index, folder, deadline):
        remaining_time(deadline)
        with pdf_lock:
            page = self.pdfium[index]
            try:
                width, height = page.get_size()
                if min(width, height) <= 0:
                    raise OCRFailure('У страницы PDF некорректный размер.')
                bitmap = page.render(scale=min(200 / 72, 2400 / max(width, height)), grayscale=True)
                try:
                    with bitmap.to_pil() as image:
                        return prepare_frame(image, folder / 'prepared.png')
                finally:
                    bitmap.close()
            finally:
                page.close()

def read_pdf_pages(document, text, total, folder, cache, deadline, force_ocr, on_progress):
    client = OpenAIClient().setup()
    page_lock = threading.Lock()
    ocr_pages = 0

    def reserve_ocr_page():
        nonlocal ocr_pages
        with page_lock:
            ocr_pages += 1
            if ocr_pages > 300:
                raise OCRFailure('В документе больше 300 страниц для OCR. Разделите учебник на части.')

    def process(index):
        remaining_time(deadline)
        cached = cached_page(cache, index, text.limit)
        if cached is not None:
            if cached['ocr']:
                reserve_ocr_page()
            return cached['text'], 'Прочитан сохранённый текст'
        try:
            try:
                native = ''
                if not force_ocr:
                    native = document.text(index)
            except Exception:
                native = ''
            letters = 0
            for character in native:
                if character.isalpha():
                    letters += 1
            needs_ocr = force_ocr or letters < 40
            if needs_ocr:
                reserve_ocr_page()
                page_folder = folder / str(index + 1)
                page_folder.mkdir()
                try:
                    has_ink = document.render(index, page_folder, deadline)
                    page_text = recognize(page_folder / 'prepared.png', has_ink, client, deadline)
                finally:
                    shutil.rmtree(page_folder, ignore_errors=True)
                stage = 'Распознавание PDF'
            else:
                page_text = native
                stage = 'Чтение текста PDF'
            if len(clean_text(page_text)) > text.limit:
                raise text_too_large(text.limit)
            save_page(cache, index, page_text, needs_ocr)
            return page_text, stage
        except Exception as error:
            if isinstance(error, OCRFailure):
                raise
            raise OCRFailure(f'Не удалось обработать страницу {index + 1} PDF. Проверьте файл или повторите обработку.') from None
    pool = ThreadPoolExecutor(max_workers=2)
    pending = {}
    try:
        for index in range(min(2, total)):
            pending[index] = pool.submit(process, index)
        for index in range(total):
            remaining_time(deadline)
            if on_progress:
                on_progress(index, total, f'Обработка страницы {index + 1} из {total}')
            page_text, stage = pending.pop(index).result()
            text.add(page_text)
            if on_progress:
                on_progress(index + 1, total, stage)
            if index + 2 < total:
                pending[index + 2] = pool.submit(process, index + 2)
    finally:
        for future in pending.values():
            future.cancel()
        pool.shutdown(wait=True, cancel_futures=True)

def extract_pdf(content, on_progress=None, force_ocr=False, max_text_chars=600000, languages='rus+eng', page_cache_dir=None):
    if not isinstance(content, bytes) or not content:
        raise OCRFailure('Загружен пустой PDF.')
    text = TextBuffer().setup(max_text_chars)
    languages = check_languages(languages)
    deadline = time.monotonic() + 3600
    document = PdfDocument().setup(content)
    try:
        total = document.page_count()
        if total > 1000:
            raise OCRFailure('В PDF больше 1000 страниц. Разделите учебник на части.')
        if not total:
            raise OCRFailure('В PDF нет страниц.')
        cache = cache_folder(page_cache_dir, content, {'kind': 'pdf', 'languages': languages, 'force_ocr': bool(force_ocr), 'raster_side': 2400, 'engine': 'pdfium', 'ocr_engine': 'openai', 'ocr_model': 'gpt-6-luna'})
        if on_progress:
            on_progress(0, total, 'Чтение PDF')
        with tempfile.TemporaryDirectory(prefix='study-ocr-') as name:
            read_pdf_pages(document, text, total, Path(name), cache, deadline, force_ocr, on_progress)
    finally:
        document.close()
    return text.result()

def extract_image(content, on_progress=None, max_text_chars=600000, languages='rus+eng', page_cache_dir=None):
    if not isinstance(content, bytes) or not content:
        raise OCRFailure('Загружено пустое изображение.')
    text = TextBuffer().setup(max_text_chars)
    languages = check_languages(languages)
    deadline = time.monotonic() + 3600
    with warnings.catch_warnings():
        warnings.simplefilter('error', Image.DecompressionBombWarning)
        try:
            source = Image.open(io.BytesIO(content))
        except Exception as error:
            if isinstance(error, (Image.DecompressionBombError, Image.DecompressionBombWarning)):
                raise OCRFailure('Изображение слишком большое: максимум 40 миллионов пикселей на страницу.') from None
            raise OCRFailure('Не удалось прочитать изображение. Проверьте файл и сохраните его в PNG, JPEG, WEBP или TIFF.') from None
        with source:
            try:
                total = getattr(source, 'n_frames', 1)
            except Exception:
                raise OCRFailure('Не удалось прочитать страницы изображения. Проверьте файл.') from None
            if source.format not in {'PNG', 'JPEG', 'WEBP', 'TIFF'}:
                raise OCRFailure('Для OCR поддерживаются изображения PNG, JPEG, WEBP и TIFF.')
            if source.format != 'TIFF' and total != 1:
                raise OCRFailure('Анимированные изображения не поддерживаются. Загрузите отдельные страницы или TIFF.')
            if total > 300:
                raise OCRFailure('В изображении больше 300 страниц. Разделите его на части.')
            if on_progress:
                on_progress(0, total, 'Подготовка изображения')
            cache = cache_folder(page_cache_dir, content, {'kind': 'image', 'languages': languages, 'raster_side': 2400, 'ocr_engine': 'openai', 'ocr_model': 'gpt-6-luna'})
            with tempfile.TemporaryDirectory(prefix='study-ocr-') as name:
                folder = Path(name)
                client = OpenAIClient().setup()
                for index in range(total):
                    remaining_time(deadline)
                    if on_progress:
                        on_progress(index, total, f'Распознавание страницы {index + 1} из {total}')
                    cached = cached_page(cache, index, max_text_chars)
                    if cached is not None:
                        text.add(cached['text'])
                        if on_progress:
                            on_progress(index + 1, total, 'Прочитан сохранённый текст')
                        continue
                    try:
                        source.seek(index)
                        raster = folder / 'prepared.png'
                        has_ink = prepare_frame(source, raster)
                        page_text = recognize(raster, has_ink, client, deadline)
                        text.add(page_text)
                        save_page(cache, index, page_text, True)
                        raster.unlink(missing_ok=True)
                    except Exception as error:
                        if isinstance(error, OCRFailure):
                            raise
                        if isinstance(error, (Image.DecompressionBombError, Image.DecompressionBombWarning)):
                            raise OCRFailure('Изображение слишком большое: максимум 40 миллионов пикселей на страницу.') from None
                        raise OCRFailure('Не удалось прочитать изображение. Проверьте файл и качество скана.') from None
                    if on_progress:
                        on_progress(index + 1, total, 'Распознавание изображения')
    return text.result()

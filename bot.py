from contextlib import closing
import hashlib
import json
import logging
import os
import secrets
from pathlib import Path
import sqlite3
import threading
import time
import requests
from generation import GenerationError
from study import DomainError

log = logging.getLogger("study_bot")

def button(text, payload):
    return {"type": "callback", "text": str(text)[:100], "payload": payload}

class MaxAPIError(requests.RequestException):
    def setup(self, status=0, code="", message=None):
        self.args = (message or "Не удалось обновить сообщение MAX.",)
        self.status = status
        self.code  = code
        return self

class MaxClient:
    def setup(self, token=None):
        self.token = token or os.environ.get("MAX_BOT_TOKEN", "").strip()
        self.base = os.environ.get("MAX_BASE_URL", "https://platform-api2.max.ru").rstrip("/")
        self.message_lock = threading.Lock()
        self.last_message =  {}
        return self

    def api(self, method, params=None, payload=None, http_method="GET"):
        operation = method if method in {"me", "updates", "messages", "answers"} else "API"
        try:
            response = requests.request(http_method, f"{self.base}/{method}", params=params,
                                        json=payload, headers={"Authorization": self.token},
                                        verify=  True,
                                        timeout=(10, 60), allow_redirects=False)
        except Exception:
            raise MaxAPIError().setup(message=f"MAX, {operation}: не удалось подключиться. Проверьте сеть и повторите попытку.") from None
        response.raise_for_status()
        try:
            result = response.json()
        except ValueError:
            raise MaxAPIError().setup(message=f"MAX, {operation}: сервер вернул ответ, который не удалось прочитать как JSON.") from None
        if isinstance(result, dict) and result.get("success") is False:
            raise MaxAPIError().setup(response.status_code, str(result.get("code") or result.get("message") or ""))
        return result

    def message_request(self, chat_id, params, body, method):
        with self.message_lock:
            now = time.monotonic()
            previous  = self.last_message.get(str(chat_id))
            if previous is not None:
                time.sleep(max(0, 0.55 - (now - previous)))
            self.last_message[str(chat_id)] = time.monotonic()
            return self.api("messages", params, body, method)

    def message_body(self, text, buttons):
        if len(text) > 3500:
            raise ValueError("Текст одной страницы не должен превышать 3500 символов.")
        rows = buttons or [[button("В меню", "menu")]]
        return {"text": text, "attachments": [{"type": "inline_keyboard", "payload": {"buttons": rows}}]}

    def send(self, chat_id, text, buttons=None):
        result = self.message_request(chat_id, {"chat_id": chat_id}, self.message_body(text, buttons), "POST")
        message_id = result.get("message", {}).get("body", {}).get("mid")
        if not isinstance(message_id, str) or not message_id:
            raise MaxAPIError().setup()
        return message_id

    def edit(self, chat_id, message_id, text, buttons=None):
        result =  self.message_request(chat_id, {"message_id": message_id}, self.message_body(text, buttons), "PUT")
        if result.get("success") is not True:
            raise MaxAPIError().setup()

    def delete(self, chat_id, message_id):
        result = self.message_request(chat_id, {"message_id": message_id}, None, "DELETE")
        if result.get("success") is not True:
            raise MaxAPIError().setup()

    def answer(self, callback_id):
        if callback_id:
            self.api("answers", {"callback_id": callback_id}, {"notification": "Принято"}, "POST")

class MaxBot:
    def setup(self, service, client=None):
        self.service = service
        self.client = client or MaxClient().setup()
        self.card_lock=  threading.RLock()
        self.update_context = threading.local()
        return self

    def menu(self, user_id):
        return [[button("Тесты с монетами", "tests"), button("Квизы", "works")],
                [button("Баланс", "balance"), button("Мои результаты", "results")],
                [button("Магазин", "shop"), button("Мои сертификаты", "requests")],
                [button("Продолжить работу", "resume"), button("Мой профиль", "profile")]]

    def registration(self, chat_id, user_id, text, callback_id=None):
        user = self.service.user(user_id)
        if user.get("registered"):
            return False
        key = f"registration:{user_id}"
        state  = self.service.get_meta(key) or {"step": "name"}
        command = text.strip().lstrip("/").lower()
        controls = {"start", "menu", "начать", "старт", "привет", "меню", "register", "регистрация"}
        if command in {"register", "регистрация"}:
            state = {"step": "name"}
        is_control =  callback_id is not None or command in controls or ":" in command or text.startswith("/")
        rows = [[button("Начать заново", "register")]]
        if state.get("step") == "name":
            if not is_control:
                try:
                    name = self.service.validate_student_name(text)
                except DomainError as error:
                    self.send(chat_id, str(error) + "\n\nНапиши ФИО, например: Иванов Иван Иванович.", rows)
                    return True
                state = {"step": "class", "name": name}
                self.service.set_meta(key, state)
                self.send(chat_id, f"Спасибо, {name}!\nТеперь напиши свой класс, например 10А.", rows)
                return True
            self.service.set_meta(key, state)
            self.send(chat_id, "Привет! Сначало надо зарегистрироваться.\nНапиши фамилию имя и отчество. Если отчества нет, можно без него.\n\nНапример: Иванов Иван Иванович.", rows)
            return True
        if not is_control:
            try:
                user=  self.service.register_student(user_id, state["name"], text)
            except DomainError as error:
                self.send(chat_id, str(error) + "\n\nНапиши класс, например: 10А.", rows)
                return True
            self.service.set_meta(key, None)
            self.send(chat_id, f"Регистрация завершена.\n{user['full_name']}\nКласс: {user['group_name']}\n\nВыбери действие.", self.menu(user_id))
            return True
        self.send(chat_id, f"ФИО: {state.get('name', '')}\nТеперь напиши свой класс, например: 10А.", rows)
        return True

    def send(self, chat_id, text, rows=None):
        with self.card_lock:
            key = f"bot_card:{chat_id}"
            old = self.service.get_meta(key, {})
            pages = []
            for index in range(0, len(text), 3400):
                pages.append(text[index:index + 3400])
            if not pages:
                pages = [""]
            card  = {"mid": old.get("mid"), "text": text, "pages": pages,
                    "buttons": rows or [[button("В меню", "menu")]], "page": 0,
                    "nonce": secrets.token_hex(8), "start_update": old.get("start_update")}
            update_key = getattr(self.update_context, "key", None)
            if update_key:
                self.service.set_meta(f"bot_pending:{chat_id}", {"update": update_key, "card": card})
            self.show_card(chat_id, card)
            if update_key:
                self.service.set_meta(f"bot_pending:{chat_id}", None)

    def restart_card(self, chat_id):
        with self.card_lock:
            key = f"bot_card:{chat_id}"
            card = self.service.get_meta(key, {})
            update_key =  getattr(self.update_context, "key", None)
            if update_key and card.get("start_update") == update_key:
                return
            if card.get("mid"):
                try:
                    self.client.delete(chat_id, card["mid"])
                except requests.RequestException as error:
                    if self.edit_failure(error) != "missing":
                        raise
            self.service.set_meta(key, {"start_update": update_key})

    def page_count(self, card):
        return max(len(card["pages"]), (len(card["buttons"]) + 26) // 27)

    def show_card(self, chat_id, card):
        index = card["page"]
        total = self.page_count(card)
        text = card["pages"][min(index, len(card["pages"]) - 1)]
        all_rows=  card["buttons"]
        rows = all_rows[index * 27:(index + 1) * 27] if len(all_rows) > 27 else list(all_rows)
        if total > 1:
            text += f"\n\nСтраница {index + 1} из {total}"
            navigation = []
            if index > 0:
                navigation.append(button("Назад", f"page:{card['nonce']}:{index - 1}"))
            if index + 1 < total:
                navigation.append(button("Далее", f"page:{card['nonce']}:{index + 1}"))
            rows = rows + [navigation]
        has_menu = False
        for row in rows:
            for item in row:
                if item.get("payload") == "menu":
                    has_menu = True
        if not has_menu:
            rows  = rows + [[button("В меню", "menu")]]
        message_id = card.get("mid")
        if message_id:
            try:
                self.client.edit(chat_id, message_id, text, rows)
            except requests.RequestException as exc:
                action = self.edit_failure(exc)
                if action == "delete":
                    try:
                        self.client.delete(chat_id, message_id)
                    except requests.RequestException as delete_error:
                        if self.edit_failure(delete_error) != "missing":
                            raise
                elif action != "missing":
                    raise
                card["mid"] = None
                self.service.set_meta(f"bot_card:{chat_id}", card)
                message_id = None
        if not message_id:
            card["mid"] = self.client.send(chat_id, text, rows)
        self.service.set_meta(f"bot_card:{chat_id}", card)

    def edit_failure(self, error):
        status =  getattr(error, "status", 0)
        code = getattr(error, "code", "")
        response = getattr(error, "response", None)
        if response is not None:
            status = response.status_code
            if status == 400:
                try:
                    data=  response.json()
                    code = str(data.get("code") or data.get("message") or "")
                except (ValueError, AttributeError):
                    code = ""
        code = code.strip().lower()
        if status == 429 or status >= 500:
            return "retry"
        if status in {404, 410} or (status in {200, 400} and code in {"message.not.found", "message_not_found"}):
            return "missing"
        if status == 400 and code in {"message.not.editable", "message_not_editable", "message.edit.time.expired"}:
            return "delete"
        return "retry"

    def change_page(self, chat_id, command):
        with self.card_lock:
            parts  = command.split(":")
            card = self.service.get_meta(f"bot_card:{chat_id}", {})
            if len(parts) != 3 or parts[1] != card.get("nonce"):
                return
            try:
                index = int(parts[2])
            except ValueError:
                return
            if 0 <= index < self.page_count(card):
                card["page"] = index
                self.show_card(chat_id, card)

    def question(self, chat_id, user_id, attempt_id):
        q = self.service.question(attempt_id, user_id)
        options = []
        rows = []
        for i, option in enumerate(q["options"]):
            options.append(f"{i + 1}. {option}")
            rows.append([button(f"Ответ {i + 1}", f"answer:{attempt_id}:{q['index']}:{i}")])
        text = f"Вопрос {q['index'] + 1}/{q['total']}\n\n{q['question']}\n\n" + "\n\n".join(options)
        self.send(chat_id, text, rows)

    def handle_command(self, chat_id, user_id, text, callback_id=None):
        command = text.strip().lstrip("/")
        user = self.service.user(user_id, chat_id=chat_id)
        verb = command.split(maxsplit=1)[0].lower() if command else "menu"
        if verb in {"start", "старт", "начать"}:
            self.restart_card(chat_id)
        if self.registration(chat_id, user_id, text, callback_id):
            return
        if command.startswith("page:"):
            self.change_page(chat_id, command)
            return
        try:
            if verb in {"start", "menu", "начать", "старт", "привет"}:
                self.send(chat_id, "Учебный бот: квизы, тесты с монетами и сертификаты.\n"
                          "За верный ответ в тесте — 1 монета. До 10 в день, 50 в неделю "
                          "и 250 за триместр. Ежедневные тесты доступны по будням через неделю.", self.menu(user_id))
            elif verb in {"id", "myid", "profile", "register", "профиль"}:
                self.send(chat_id, f"{user['full_name']}\nКласс: {user['group_name']}\nТвой ID: {user_id}\n\nЕсли тут ошибка напиши учителю, он поправит.", self.menu(user_id))
            elif verb in {"tests", "тесты"}:
                tests=  self.service.available_tests(user_id)
                rows = []
                for test in tests:
                    rows.append([button(test["topic"], f"work:{test['id']}")])
                rows.append([button("Ежедневный тест", "mini")])
                self.send(chat_id, "Выбери тест:" if tests else "Учитель пока не добавил тесты.", rows)
            elif verb in {"test", "mini"}:
                quiz = self.service.available_mini(group_id=user.get("group_id"))
                if not quiz:
                    self.send(chat_id, "Сегодня ежедневный тест недоступен. Такие тесты бывают по будням "
                              "через неделю. Учитель ещё должен выбрать учебник и тему.", self.menu(user_id))
                else:
                    attempt = self.service.start_attempt(user_id, quiz["id"])
                    self.question(chat_id, user_id, attempt["id"])
            elif verb == "works":
                works  = self.service.available_assessments(user_id)
                rows = []
                for quiz in works[:30]:
                    rows.append([button(quiz["topic"], f"work:{quiz['id']}")])
                rows.append([button("В меню", "menu")])
                self.send(chat_id, "Выбери квиз:" if works else "Доступных квизов пока нет.", rows)
            elif command.startswith("work:"):
                attempt = self.service.start_attempt(user_id, int(command.split(":")[1]))
                self.question(chat_id, user_id, attempt["id"])
            elif verb == "resume":
                attempt = self.service.active_attempt(user_id)
                if attempt:
                    self.question(chat_id, user_id, attempt["id"])
                else:
                    self.send(chat_id, "Начатых работ нет.", self.menu(user_id))
            elif command.startswith("answer:"):
                _, aid, index, option = command.split(":")
                result = self.service.answer(int(aid), user_id, int(index), int(option))
                if result["done"]:
                    self.send(chat_id, f"Работа завершена: {result['score']}/{result['total']}.\n"
                              f"Начислено монет: {result['coins']}. Баланс: {result['balance']}.\n"
                              "Разбор доступен в «Мои результаты».", self.menu(user_id))
                else:
                    self.question(chat_id, user_id, int(aid))
            elif verb == "balance":
                self.send(chat_id, f"Баланс: {user['coins']} монет.", self.menu(user_id))
            elif verb == "results":
                results = []
                for result in self.service.results():
                    if str(result["user_id"]) == str(user_id):
                        results.append(result)
                lines = []
                for r in results[:10]:
                    lines.append(f"{r['topic']}: {r['score']}/{r['total']}")
                    for e in r.get("errors", []):
                        if isinstance(e, dict):
                            lines.append(f"  • {e.get('question', '')}\n    {e.get('explanation', '')}")
                self.send(chat_id, "\n".join(lines) or "Результатов пока нет.", self.menu(user_id))
            elif verb == "shop":
                items = self.service.shop(user_id)
                shop_text = "Магазин сертификатов. Лимиты на одного ученика за триместр.\n" \
                            "Покупку и использование подтверждает учитель. Монеты остаются и после конца триместра."
                descriptions = []
                rows = []
                for item in items:
                    if item.get("description"):
                        descriptions.append(f"{item['title']}: {item['description']}")
                    rows.append([button(f"{item['price']} мон. · {item['title']} / {item['remaining']} шт.", f"buy:{item['key']}")])
                rows.append([button("В меню", "menu")])
                if descriptions:
                    shop_text += "\n\n" + "\n\n".join(descriptions)
                if not items:
                    shop_text = "В магазине пока нет доступных сертификатов. Учитель добавит их позже."
                self.send(chat_id, shop_text, rows)
            elif command.startswith("buy:"):
                request = self.service.buy(user_id, command.split(":", 1)[1],
                                           idempotency_key=str(callback_id) if callback_id else None)
                self.send(chat_id, f"Заявка №{request['id']} создана. Монеты списаны. "
                          "Когда учитель одобрит заявку, сертификат можно использовать. "
                          "Если отклонит — монеты вернутся.", self.menu(user_id))
            elif verb == "requests":
                labels  = {"pending": "Ожидает решения", "approved": "Одобрен", "rejected": "Отклонён", "redeemed": "Использован"}
                lines = []
                for request in self.service.list_requests(user_id):
                    status = labels.get(request['status'], request['status'])
                    lines.append(f"№{request['id']} — {request['title']} — {status}")
                self.send(chat_id, "\n".join(lines) or "Сертификатов пока нет.", self.menu(user_id))
            else:
                self.send(chat_id, "Выбери действие в меню.", self.menu(user_id))
        except Exception as error:
            if isinstance(error, (DomainError, GenerationError)):
                self.send(chat_id, str(error), self.menu(user_id))
            elif isinstance(error, (ValueError, IndexError, KeyError)):
                self.send(chat_id, "Команда устарела или записана неверно. Открой меню и попробуй снова.", self.menu(user_id))
            else:
                raise

    def handle_update(self, update):
        callback_id = None
        kind = update.get("update_type")
        if kind == "bot_started":
            actor, chat, command = update.get("user") or {}, update.get("chat_id"), "start"
        elif kind in {"message_created", "message_callback"}:
            message =  update.get("message") or {}
            if message.get("recipient", {}).get("chat_type") not in {None, "dialog"}:
                return
            chat = message.get("recipient", {}).get("chat_id")
            if kind == "message_callback":
                callback = update.get("callback") or {}
                actor, command = callback.get("user") or {}, callback.get("payload") or "menu"
                callback_id = callback.get("callback_id")
                try:
                    self.client.answer(callback.get("callback_id"))
                except requests.RequestException:
                    log.warning("Не удалось подтвердить нажатие кнопки")
            else:
                actor=  message.get("sender") or {}
                command = message.get("body", {}).get("text") or "menu"
        else:
            return
        if chat is None or actor.get("user_id") is None or actor.get("is_bot"):
            return
        uid = actor["user_id"]
        name = (str(actor.get("first_name") or "") + " " + str(actor.get("last_name") or "")).strip()
        self.service.user(uid, name=name, chat_id=chat)
        profile  = dict(actor)
        if update.get("user_locale"):
            profile["user_locale"] = update["user_locale"]
        self.service.remember_profile(uid, profile)
        update_key = hashlib.sha256(json.dumps(update, ensure_ascii=False, sort_keys=True).encode()).hexdigest()
        with self.card_lock:
            pending = self.service.get_meta(f"bot_pending:{chat}")
            if pending and pending.get("update") == update_key:
                card = pending["card"]
                saved =  self.service.get_meta(f"bot_card:{chat}", {})
                if saved.get("nonce") == card.get("nonce") and saved.get("mid"):
                    card = saved
                self.show_card(chat, card)
                self.service.set_meta(f"bot_pending:{chat}", None)
                return
            self.update_context.key = update_key
            try:
                self.handle_command(chat, uid, command, callback_id=callback_id)
            finally:
                del self.update_context.key

class UpdateInbox:
    def setup(self, path):
        self.path = str(Path(path).resolve())
        if not Path(self.path).is_file():
            raise FileNotFoundError("Не найдена готовая база данных: " + self.path)
        self.uri = Path(self.path).as_uri() + "?mode=rw"
        return self

    def put(self, update):
        payload=  json.dumps(update, ensure_ascii=False, sort_keys=True)
        key = hashlib.sha256(payload.encode()).hexdigest()
        with closing(sqlite3.connect(self.uri, uri=True, timeout=30)) as conn, conn:
            conn.execute("INSERT OR IGNORE INTO inbox(key,payload) VALUES(?,?)", (key, payload))

    def drain(self, bot, stop):
        while not stop.is_set():
            with closing(sqlite3.connect(self.uri, uri=True, timeout=30)) as conn, conn:
                rows = conn.execute("SELECT key,payload FROM inbox WHERE done=0 ORDER BY rowid LIMIT 25").fetchall()
            for key, payload in rows:
                if stop.is_set():
                    return
                try:
                    bot.handle_update(json.loads(payload))
                except Exception:
                    log.warning("Не получилось ответить. Событие сохранили, попробуем ещё раз.")
                    stop.wait(5)
                    break
                else:
                    with closing(sqlite3.connect(self.uri, uri=True, timeout=30)) as conn, conn:
                        conn.execute("UPDATE inbox SET done=1 WHERE key=?", (key,))
                        conn.execute("DELETE FROM inbox WHERE done=1 AND rowid < (SELECT COALESCE(MAX(rowid),0)-10000 FROM inbox)")
            if not rows:
                stop.wait(0.5)

def poll(bot, inbox, stop):
    while not stop.is_set():
        try:
            result = bot.client.api("updates", {"timeout": 30, "limit": 100,
                                               "marker": bot.service.get_meta("marker")})
            for update in result.get("updates", []):
                inbox.put(update)
            bot.service.set_meta("marker", result.get("marker", bot.service.get_meta("marker")))
        except Exception:
            log.warning("Не удалось получить или сохранить события MAX. Попробуем ещё через 5 секунд.")
            stop.wait(5)

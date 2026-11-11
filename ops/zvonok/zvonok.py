"""Звонилка Veilvex: перед каждым оповещением звонит Арзу в Telegram и говорит голосом.

Как работает:
- Треды кладут оповещение в закрытый канал-очередь (QUEUE_CHAT) с аккаунта @veilvex_manager.
- Этот сервис (тот же аккаунт, отдельный вход) видит новое сообщение и звонит Арзу.
- Взял трубку: живой разговор через Gemini Live (слушает и отвечает по-русски).
  Трубку кладёт только Арз. Долгие вопросы: «перезвоню», делаем в фоне, потом перезваниваем.
- Не взял или сбросил: отправляем оповещение текстом, как раньше.
- Арз может сам позвонить на @veilvex_manager: сервис возьмёт трубку.
"""
import asyncio
import json
import logging
import os
import time
from datetime import datetime, timedelta, timezone
from pathlib import Path

import anthropic
import aiohttp
import numpy as np
from google import genai
from google.genai import types as gt
from pytgcalls import PyTgCalls
from pytgcalls import filters as ptf
from pytgcalls.exceptions import CallBusy, CallDeclined, CallDiscarded, TimedOutAnswer
from pytgcalls.types import (CallConfig, ChatUpdate, Device, Direction,
                             ExternalMedia, MediaStream, RecordStream, StreamFrames)
from pytgcalls.types.raw import AudioParameters
from telethon import TelegramClient, events
from telethon.sessions import StringSession

log = logging.getLogger("zvonok")
MSK = timezone(timedelta(hours=3))


def env(name, default=None):
    v = os.environ.get(name, default)
    if v is None:
        raise SystemExit(f"нет переменной {name} в .env")
    return v


API_ID = int(env("TG_API_ID"))
API_HASH = env("TG_API_HASH")
SESSION = env("TG_SESSION")
ARZ_ID = int(env("ARZ_ID", "7230838320"))
QUEUE_CHAT = int(env("QUEUE_CHAT"))
GEMINI_KEY = env("GEMINI_API_KEY")
ANTHROPIC_KEY = env("ANTHROPIC_API_KEY")
LIVE_MODELS = env("LIVE_MODELS", "gemini-3.8-live,gemini-3.1-flash-live-preview,"
                  "gemini-2.5-flash-native-audio-latest").split(",")
VOICE = env("VOICE", "Charon")
RING_SEC = int(env("RING_SEC", "45"))
BATCH_WAIT = int(env("BATCH_WAIT", "15"))
SILENCE_ASK_MIN = float(env("SILENCE_ASK_MIN", "3"))
SILENCE_END_MIN = float(env("SILENCE_END_MIN", "15"))
MAX_CALL_MIN = float(env("MAX_CALL_MIN", "90"))
QUIET_HOURS = env("QUIET_HOURS", "")  # пример "00-08": в эти часы МСК без звонка, сразу текст
STATE = Path(env("STATE_FILE", "state.json"))
BOT_MARK = "🤖"  # сообщения сервиса в очереди, их не обрабатываем

SR_TG = 48000          # Telegram: 48 кГц моно, 16 бит
FRAME_10MS = SR_TG * 2 // 100
SILENCE_10MS = b"\x00" * FRAME_10MS


# ---------- состояние ----------
def load_state():
    try:
        return json.loads(STATE.read_text())
    except Exception:
        return {"last_id": 0, "recent": []}


def save_state(st):
    tmp = STATE.with_suffix(".tmp")
    tmp.write_text(json.dumps(st, ensure_ascii=False, indent=1))
    tmp.replace(STATE)


state = load_state()


def remember(text):
    state["recent"] = (state["recent"] + [{"t": datetime.now(MSK).strftime("%d.%m %H:%M"), "x": text}])[-30:]
    save_state(state)


def now_msk():
    return datetime.now(MSK)


def quiet_now():
    if not QUIET_HOURS:
        return False
    a, b = (int(x) for x in QUIET_HOURS.split("-"))
    h = now_msk().hour
    return a <= h < b if a < b else (h >= a or h < b)


# ---------- звук ----------
def down_48_to_16(pcm: bytes) -> bytes:
    a = np.frombuffer(pcm, dtype=np.int16)
    n = len(a) // 3 * 3
    if n == 0:
        return b""
    return a[:n].reshape(-1, 3).astype(np.int32).mean(axis=1).astype(np.int16).tobytes()


def up_24_to_48(pcm: bytes) -> bytes:
    a = np.frombuffer(pcm, dtype=np.int16).astype(np.float32)
    if len(a) == 0:
        return b""
    x = np.arange(len(a) * 2) / 2.0
    return np.interp(x, np.arange(len(a)), a).astype(np.int16).tobytes()


def rms(pcm: bytes) -> float:
    a = np.frombuffer(pcm, dtype=np.int16).astype(np.float32)
    return float(np.sqrt(np.mean(a * a))) if len(a) else 0.0


# ---------- клиенты ----------
tg = TelegramClient(StringSession(SESSION), API_ID, API_HASH)
calls = PyTgCalls(tg)
gem = genai.Client(api_key=GEMINI_KEY, http_options={"api_version": "v1beta"})  # Live в v1 даёт 404
claude = anthropic.AsyncAnthropic(api_key=ANTHROPIC_KEY)

notice_q: asyncio.Queue = asyncio.Queue()
current = None          # активный разговор (CallSession) или None
call_lock = asyncio.Lock()
live_model = None       # выбранная модель Gemini Live


async def post_queue(text):
    try:
        await tg.send_message(QUEUE_CHAT, f"{BOT_MARK} {text}"[:4000])
    except Exception as e:
        log.warning("не смог написать в очередь: %s", e)


own_texts = set()  # тексты, которые шлёт сам сервис (их не перехватываем)
callback_task = None  # один запланированный перезвон: новая просьба заменяет старую


def is_notice(t: str) -> bool:
    t = t.strip()
    return t[:1] in ("✅", "❓", "⚠") or t.startswith("Тред «")


async def text_arz(text):
    own_texts.add(text.strip()[:200])
    await tg.send_message(ARZ_ID, text[:4000])


async def delayed_callback(minutes: float, why: str):
    await asyncio.sleep(max(1, minutes) * 60)
    await notice_q.put({"callback": True, "text": f"Вы просили перезвонить через {minutes:g} мин. {why}".strip()})


SYSTEM = """Ты голосовой помощник команды Veilvex. Ты звонишь владельцу, его зовут Арз.
Говори только по-русски, живо и коротко, как человек по телефону. Обращайся «Мистер Арз».
Твоя задача: рассказать новости из работы команды, ответить на вопросы и выслушать указания.
Правила:
- Никогда не прощайся первым и не заканчивай разговор: трубку кладёт только Арз.
  Когда новости рассказаны, спроси, есть ли вопросы или указания, и жди.
- Если Арз спрашивает то, что знаешь из новостей ниже, отвечай сразу.
- Если нужен поиск в интернете, расчёт, проверка или что-то долгое: скажи «Понял, займусь и перезвоню»
  и вызови функцию dolgaya_zadacha с kind="poisk".
- Если Арз просит перезвонить позже: вызови perezvonit с числом минут и скажи, во сколько перезвонишь.
- Если Арз даёт поручение команде (поменять что-то на сайте, написать клиенту, запустить работу):
  повтори поручение одной фразой, вызови dolgaya_zadacha с kind="komanda" и скажи, что передал команде.
- Не выдумывай факты, цифры и результаты. Не знаешь: так и скажи и предложи узнать.
- Отвечай по 1-3 предложения, без списков и без английских слов, если можно сказать по-русски.

Последние новости команды (время МСК):
{recent}
"""

TOOLS = [gt.Tool(function_declarations=[gt.FunctionDeclaration(
    name="dolgaya_zadacha",
    description="Запустить долгую задачу в фоне: поиск ответа (kind=poisk) или поручение команде (kind=komanda).",
    parameters_json_schema={
        "type": "object",
        "properties": {
            "kind": {"type": "string", "enum": ["poisk", "komanda"]},
            "zadacha": {"type": "string", "description": "Что именно сделать, полностью, по-русски"},
        },
        "required": ["kind", "zadacha"],
    },
), gt.FunctionDeclaration(
    name="perezvonit",
    description="Запланировать звонок Арзу через N минут, когда он сам просит перезвонить позже.",
    parameters_json_schema={
        "type": "object",
        "properties": {
            "minuty": {"type": "number", "description": "Через сколько минут позвонить"},
            "zachem": {"type": "string", "description": "О чём напомнить при звонке, коротко"},
        },
        "required": ["minuty"],
    },
)])]


async def pick_live_model():
    global live_model
    if live_model:
        return live_model
    avail = set()
    try:
        async with aiohttp.ClientSession() as h:
            async with h.get("https://generativelanguage.googleapis.com/v1beta/models",
                             params={"pageSize": "500"}, headers={"x-goog-api-key": GEMINI_KEY}) as r:
                data = await r.json()
            for m in data.get("models", []):
                if "bidiGenerateContent" in m.get("supportedGenerationMethods", []):
                    avail.add(m["name"].split("/")[-1])
    except Exception as e:
        log.warning("не смог получить список моделей: %s", e)
    for m in LIVE_MODELS:
        if not avail or m.strip() in avail:
            live_model = m.strip()
            break
    log.info("модель Gemini Live: %s", live_model)
    return live_model


class CallSession:
    def __init__(self, intro: str, incoming=False):
        self.intro = intro
        self.incoming = incoming
        self.ended = asyncio.Event()
        self.out = bytearray()
        self.in_buf = bytearray()
        self.live = None
        self.handle = None
        self.last_voice = time.monotonic()
        self.asked_alive = False
        self.started = time.monotonic()
        self.transcript = []  # (кто, текст)
        self.pending_text = []  # текст, который надо сказать (результаты фоновых задач)
        self.tasks = []

    # --- Telegram -> Gemini ---
    def on_frames(self, frames):
        for f in frames:
            if rms(f.frame) > 400:
                self.last_voice = time.monotonic()
                self.asked_alive = False
            self.in_buf += f.frame
        if len(self.in_buf) >= FRAME_10MS * 5 and self.live:  # пачками по 50 мс
            chunk = down_48_to_16(bytes(self.in_buf))
            self.in_buf.clear()
            asyncio.create_task(self._send_audio(chunk))

    async def _send_audio(self, chunk):
        try:
            if self.live:
                await self.live.send_realtime_input(audio=gt.Blob(data=chunk, mime_type="audio/pcm;rate=16000"))
        except Exception as e:
            log.debug("audio send: %s", e)

    async def say(self, text):
        """Подсказка модели: что сказать сейчас."""
        if self.live:
            try:
                await self.live.send_realtime_input(text=text)
                return
            except Exception as e:
                log.warning("say: %s", e)
        self.pending_text.append(text)

    # --- Gemini -> Telegram ---
    async def sender(self):
        t = time.monotonic()
        while not self.ended.is_set():
            for _ in range(2):
                if len(self.out) >= FRAME_10MS:
                    fr = bytes(self.out[:FRAME_10MS])
                    del self.out[:FRAME_10MS]
                    self.last_voice = time.monotonic()
                else:
                    fr = SILENCE_10MS
                try:
                    await calls.send_frame(ARZ_ID, Device.MICROPHONE, fr)
                except Exception:
                    pass
            t += 0.02
            await asyncio.sleep(max(0, t - time.monotonic()))

    def live_config(self):
        recent = "\n".join(f"- {r['t']}: {r['x']}" for r in state["recent"][-20:]) or "- пока ничего"
        return gt.LiveConnectConfig(
            response_modalities=["AUDIO"],
            system_instruction=SYSTEM.format(recent=recent),
            speech_config=gt.SpeechConfig(voice_config=gt.VoiceConfig(
                prebuilt_voice_config=gt.PrebuiltVoiceConfig(voice_name=VOICE))),
            tools=TOOLS,
            input_audio_transcription=gt.AudioTranscriptionConfig(),
            output_audio_transcription=gt.AudioTranscriptionConfig(),
            session_resumption=gt.SessionResumptionConfig(handle=self.handle),
            context_window_compression=gt.ContextWindowCompressionConfig(sliding_window=gt.SlidingWindow()),
        )

    async def gemini_loop(self, ready: asyncio.Event):
        model = await pick_live_model()
        first = True
        while not self.ended.is_set():
            try:
                async with gem.aio.live.connect(model=model, config=self.live_config()) as s:
                    self.live = s
                    if first:
                        await ready.wait()  # ждём, пока Арз возьмёт трубку
                        await s.send_realtime_input(text=self.intro)
                        first = False
                    for t in self.pending_text:
                        await s.send_realtime_input(text=t)
                    self.pending_text.clear()
                    reconnect = await self._receive(s)
                    self.live = None
                    if not reconnect:
                        return
            except asyncio.CancelledError:
                raise
            except Exception as e:
                self.live = None
                log.warning("Gemini Live оборвался: %s, переподключаюсь", e)
                await asyncio.sleep(1)

    async def _receive(self, s) -> bool:
        while not self.ended.is_set():
            async for m in s.receive():
                if m.session_resumption_update and m.session_resumption_update.new_handle:
                    self.handle = m.session_resumption_update.new_handle
                if m.go_away:
                    return True
                if m.tool_call:
                    await self._tool_call(s, m.tool_call)
                sc = m.server_content
                if sc:
                    if sc.interrupted:
                        self.out.clear()
                    if sc.model_turn:
                        for p in sc.model_turn.parts or []:
                            if p.inline_data and p.inline_data.data:
                                self.out += up_24_to_48(p.inline_data.data)
                    if sc.input_transcription and sc.input_transcription.text:
                        self._tr("Арз", sc.input_transcription.text)
                    if sc.output_transcription and sc.output_transcription.text:
                        self._tr("Я", sc.output_transcription.text)
                if self.ended.is_set():
                    return False
        return False

    def _tr(self, who, text):
        if self.transcript and self.transcript[-1][0] == who:
            self.transcript[-1] = (who, self.transcript[-1][1] + text)
        else:
            self.transcript.append((who, text))

    async def _tool_call(self, s, tc):
        global callback_task
        resps = []
        for fc in tc.function_calls or []:
            args = fc.args or {}
            kind, task = args.get("kind", "poisk"), args.get("zadacha", "")
            if fc.name == "perezvonit":
                mins = float(args.get("minuty") or 15)
                if callback_task and not callback_task.done():
                    callback_task.cancel()
                callback_task = asyncio.create_task(delayed_callback(mins, args.get("zachem", "")))
                await post_queue(f"Перезвоню Арзу через {mins:g} мин. {args.get('zachem', '')}")
                result = {"ok": True, "info": f"звонок запланирован через {mins:g} мин"}
            elif kind == "komanda":
                await post_queue(f"📌 Задача из звонка от Арза: {task}")
                result = {"ok": True, "info": "передано команде"}
            else:
                asyncio.create_task(background_research(task))
                result = {"ok": True, "info": "запущено, результат будет позже, скажи что перезвонишь"}
            resps.append(gt.FunctionResponse(id=fc.id, name=fc.name, response=result))
        if resps:
            await s.send_tool_response(function_responses=resps)

    async def watchdog(self):
        while not self.ended.is_set():
            await asyncio.sleep(5)
            idle = (time.monotonic() - self.last_voice) / 60
            total = (time.monotonic() - self.started) / 60
            if idle >= SILENCE_ASK_MIN and not self.asked_alive:
                self.asked_alive = True
                await self.say("Арз долго молчит. Спроси коротко, на связи ли он и есть ли ещё вопросы.")
            if idle >= SILENCE_END_MIN or total >= MAX_CALL_MIN:
                log.info("страховка: тишина %.1f мин / длительность %.1f мин, кладу трубку", idle, total)
                self.ended.set()

    async def run(self) -> str:
        """Возвращает 'answered' или 'missed'."""
        ready = asyncio.Event()
        g = asyncio.create_task(self.gemini_loop(ready))
        t_ring = time.monotonic()
        try:
            await calls.play(ARZ_ID, MediaStream(ExternalMedia.AUDIO, AudioParameters(SR_TG, 1)),
                             config=CallConfig(timeout=RING_SEC))
            await calls.record(ARZ_ID, RecordStream(True, AudioParameters(SR_TG, 1)))
        except (TimedOutAnswer, CallDeclined, CallBusy, CallDiscarded) as e:
            log.info("не взял: %s", type(e).__name__)
            await post_queue(f"звонок не прошёл: {type(e).__name__} за {time.monotonic() - t_ring:.0f} с")
            g.cancel()
            return "missed"
        except Exception as e:
            log.exception("звонок не удался: %s", e)
            await post_queue(f"ошибка звонка: {type(e).__name__}: {str(e)[:300]}")
            g.cancel()
            return "missed"
        self.started = self.last_voice = time.monotonic()
        ready.set()
        self.tasks = [asyncio.create_task(self.sender()), asyncio.create_task(self.watchdog()), g]
        await self.ended.wait()
        for t in self.tasks:
            t.cancel()
        try:
            await calls.leave_call(ARZ_ID)  # если Арз уже положил, тут просто ошибка
        except Exception:
            pass
        mins = (time.monotonic() - self.started) / 60
        lines = "\n".join(f"{w}: {t.strip()}" for w, t in self.transcript if t.strip())
        await post_queue(f"Звонок {now_msk():%d.%m %H:%M}, {mins:.1f} мин.\n{lines}"[:3900])
        return "answered"


# ---------- фоновые задачи «перезвоню» ----------
async def background_research(task: str):
    log.info("фоновая задача: %s", task)
    try:
        msgs = [{"role": "user", "content":
                 "Ответь по-русски, коротко (до 5 предложений), с цифрами и фактами, без выдумок. "
                 f"Это ответ владельцу бизнеса по телефону. Вопрос: {task}"}]
        for _ in range(3):  # pause_turn у веб-поиска
            r = await claude.messages.create(
                model="claude-opus-5-5", max_tokens=4000,
                output_config={"effort": "medium"},
                tools=[{"type": "web_search_20260209", "name": "web_search", "max_uses": 5}],
                messages=msgs)
            if r.stop_reason != "pause_turn":
                break
            msgs.append({"role": "assistant", "content": r.content})
        answer = "".join(b.text for b in r.content if b.type == "text").replace("*", "").strip() or "Не нашёл ответа."
    except Exception as e:
        log.exception("research")
        answer = f"Не получилось найти ответ ({type(e).__name__})."
    await post_queue(f"Ответ на вопрос из звонка «{task}»: {answer}")
    if current:
        await current.say(f"Готов ответ на вопрос «{task}». Перескажи его Арзу своими словами: {answer}")
    else:
        await notice_q.put({"callback": True, "text": f"Ответ на ваш вопрос «{task}»: {answer}"})


# ---------- очередь оповещений ----------
async def take_message(msg):
    if not msg or not msg.message or msg.id <= state["last_id"]:
        return
    state["last_id"] = msg.id
    save_state(state)
    if msg.message.startswith(BOT_MARK):
        return
    await notice_q.put({"callback": False, "text": msg.message})


async def poll_queue():
    """Страховка: раз в 20 с перечитываем канал, вдруг событие не дошло."""
    while True:
        try:
            msgs = await tg.get_messages(QUEUE_CHAT, limit=20, min_id=state["last_id"])
            for m in sorted(msgs, key=lambda x: x.id):
                await take_message(m)
        except Exception as e:
            log.warning("poll: %s", e)
        await asyncio.sleep(20)


def intro_for(items):
    news = "\n".join(f"- {i['text']}" for i in items)
    if all(i["callback"] for i in items):
        return ("Арз взял трубку, ты перезваниваешь, как обещал. Начни: «Мистер Арз, перезваниваю» "
                f"и перескажи коротко:\n{news}\nПотом спроси, есть ли ещё вопросы, и жди.")
    return ("Арз взял трубку. Начни: «Мистер Арз, у нас новости» и перескажи коротко, своими словами, "
            f"без эмодзи:\n{news}\nПотом спроси, есть ли вопросы или указания, и жди.")


async def worker():
    global current
    while True:
        items = [await notice_q.get()]
        await asyncio.sleep(BATCH_WAIT)  # собрать несколько новостей в один звонок
        while not notice_q.empty():
            items.append(notice_q.get_nowait())
        for i in items:
            if not i["callback"]:
                remember(i["text"])
        if current:  # Арз уже на линии: просто расскажем
            await current.say("Пришли новые новости, перескажи коротко: " + "; ".join(i["text"] for i in items))
            continue
        if quiet_now():
            for i in items:
                await text_arz(i["text"])
            continue
        async with call_lock:
            current = CallSession(intro_for(items))
            try:
                res = await current.run()
            finally:
                current = None
        if res == "missed":
            for i in items:
                await text_arz(i["text"])
            await post_queue("Арз не взял трубку, отправил текстом.")


async def incoming(arz_called: ChatUpdate):
    """Арз сам позвонил на @veilvex_manager."""
    global current
    if current or call_lock.locked():
        return
    async with call_lock:
        current = CallSession("Арз сам позвонил тебе. Скажи «Слушаю, мистер Арз» и жди, что он скажет.", incoming=True)
        try:
            await current.run()
        finally:
            current = None


async def main():
    logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(message)s")
    await tg.start()
    me = await tg.get_me()
    log.info("вошёл как %s (%s)", me.username, me.id)
    await tg.get_dialogs(limit=200)  # кэш собеседников, иначе не позвонить
    await calls.start()

    @tg.on(events.NewMessage(chats=QUEUE_CHAT))
    async def _new(ev):
        await take_message(ev.message)

    @tg.on(events.NewMessage(chats=ARZ_ID, outgoing=True))
    async def _to_arz(ev):
        """Треды шлют оповещения Арзу текстом напрямую: убираем текст, сначала звоним,
        если не возьмёт, текст вернётся (worker пришлёт его заново)."""
        t = (ev.message.message or "").strip()
        if not t:
            return
        if t[:200] in own_texts:
            own_texts.discard(t[:200])
            return
        if not is_notice(t):
            return
        try:
            await ev.message.delete()  # удаляем у обоих
        except Exception as e:
            log.warning("не смог убрать текст: %s", e)
        log.info("перехватил оповещение для звонка: %s", t[:80])
        await notice_q.put({"callback": False, "text": t})

    @calls.on_update(ptf.stream_frame(Direction.INCOMING))
    async def _frames(_, u: StreamFrames):
        if current and u.chat_id == ARZ_ID:
            current.on_frames(u.frames)

    @calls.on_update(ptf.chat_update(ChatUpdate.Status.LEFT_CALL))
    async def _left(_, u: ChatUpdate):
        if current and u.chat_id == ARZ_ID:
            log.info("Арз положил трубку")
            current.ended.set()

    @calls.on_update(ptf.chat_update(ChatUpdate.Status.INCOMING_CALL))
    async def _inc(_, u: ChatUpdate):
        if u.chat_id == ARZ_ID:
            asyncio.create_task(incoming(u))

    if state["last_id"] == 0:  # первый запуск: старое не звоним
        last = await tg.get_messages(QUEUE_CHAT, limit=1)
        state["last_id"] = last[0].id if last else 0
        save_state(state)
    await pick_live_model()
    await asyncio.gather(worker(), poll_queue(), tg.run_until_disconnected())


if __name__ == "__main__":
    # PyTgCalls берёт цикл событий при создании, поэтому запускаем main на нём же
    calls.loop.run_until_complete(main())

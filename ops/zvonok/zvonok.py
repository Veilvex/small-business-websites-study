"""Звонилка Veilvex: перед каждым оповещением звонит Арзу в Telegram и говорит голосом.

Как работает:
- Треды кладут оповещение в закрытый канал-очередь (QUEUE_CHAT) с аккаунта @veilvex_manager.
- Этот сервис (тот же аккаунт, отдельный вход) видит новое сообщение и звонит Арзу.
- Взял трубку: живой разговор через Gemini Live (слушает и отвечает по-русски).
  Трубку кладёт только Арз. Долгие вопросы: «перезвоню», делаем в фоне, потом перезваниваем.
- Не взял или сбросил: отправляем оповещение текстом, как раньше.
- Арз может сам позвонить на @veilvex_manager: сервис возьмёт трубку.
- Арз пишет или шлёт голосовое в чат: отвечаем текстом или голосовым (как спросил),
  поручения уходят в канал-очередь как «📌 Задача от Арза из чата», команда их забирает.
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
NOISE_GATE = float(env("NOISE_GATE", "900"))  # пока бот говорит, тише этого (и 4× фона) = шум, не перебивает
QUIET_HOURS = env("QUIET_HOURS", "") or "22-10"  # Арз 11.11: с 22 до 10 МСК не звоним, шлём текстом
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
    q = state.get("quiet", QUIET_HOURS)  # Арз может поменять из чата (tihie_chasy)
    if not q or q == "off":
        return False
    a, b = (int(x) for x in q.split("-"))
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
    await notice_q.put({"callback": True, "force": True,  # сам просил: звоним и ночью
                        "text": f"Вы просили перезвонить через {minutes:g} мин. {why}".strip()})


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
        self.noise = 200.0    # уровень фонового шума у Арза (среднее по тихим кускам)
        self.last_out = 0.0   # когда бот последний раз говорил

    # --- Telegram -> Gemini ---
    def on_frames(self, frames):
        for f in frames:
            if rms(f.frame) > 400:
                self.last_voice = time.monotonic()
                self.asked_alive = False
            self.in_buf += f.frame
        if len(self.in_buf) >= FRAME_10MS * 5 and self.live:  # пачками по 50 мс
            raw = bytes(self.in_buf)
            self.in_buf.clear()
            r = rms(raw)
            if r < self.noise * 2.5:
                self.noise = max(50.0, self.noise * 0.95 + r * 0.05)
            bot_talks = len(self.out) > 0 or time.monotonic() - self.last_out < 0.5
            if bot_talks and r < max(NOISE_GATE, self.noise * 4):
                raw = b"\x00" * len(raw)  # фоновый шум не должен перебивать бота, перебивает только голос Арза
            chunk = down_48_to_16(raw)
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
                    self.last_voice = self.last_out = time.monotonic()
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
            # реже срабатывать на шум: начало речи с низкой чувствительностью, нужна сплошная речь 300 мс
            realtime_input_config=gt.RealtimeInputConfig(automatic_activity_detection=gt.AutomaticActivityDetection(
                start_of_speech_sensitivity=gt.StartSensitivity.START_SENSITIVITY_LOW,
                end_of_speech_sensitivity=gt.EndSensitivity.END_SENSITIVITY_LOW,
                prefix_padding_ms=300, silence_duration_ms=800)),
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
        if quiet_now() and not any(i.get("force") for i in items):  # тихие часы: текстом
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


# ---------- чат: Арз пишет или шлёт голосовое в @veilvex_manager ----------
STT_MODELS = env("STT_MODELS", "gemini-3.5-flash,gemini-2.5-flash,gemini-3.8-flash").split(",")
TTS_MODELS = env("TTS_MODELS", "gemini-3.1-flash-tts-preview,gemini-2.5-flash-preview-tts,"
                 "gemini-3.8-flash-tts").split(",")
CHAT_MODEL = env("CHAT_MODEL", "claude-opus-5-5")
CHAT_WAIT = float(env("CHAT_WAIT", "3"))  # подождать, вдруг Арз пишет несколько сообщений подряд

chat_q: asyncio.Queue = asyncio.Queue()

CHAT_SYSTEM = """Ты помощник команды Veilvex в Telegram-чате с владельцем, его зовут Арз.
Отвечай только по-русски (если Арз пишет по-азербайджански, отвечай по-азербайджански).
Пиши коротко и по-человечески, как в мессенджере: 1-4 предложения, без markdown, без звёздочек, без списков,
если можно без них. Обращайся «Мистер Арз» только в начале разговора, дальше просто по делу.
Что ты умеешь:
- Отвечать на вопросы про работу команды по новостям ниже. Не знаешь: так и скажи.
- Искать в интернете (web_search), когда нужны свежие факты, цены, новости.
- peredat_komande: когда Арз даёт поручение (сделать что-то на сайте, написать клиенту, найти, запустить
  работу, поменять настройку). Сформулируй задачу полностью, со всеми деталями из его слов,
  вызови инструмент и коротко скажи, что передал команде и они займутся.
- pozvonit: когда Арз просит позвонить ему (сейчас или через N минут). Его просьбу выполняем и ночью.
- tihie_chasy: когда Арз меняет время, когда ему не звонить. Сейчас тихие часы: {quiet} МСК
  (в это время новости приходят текстом, а его собственные звонки принимаем всегда).
- format_otveta: когда Арз просит ответить голосом / голосовым / «скажи вслух» (golos=true) или текстом
  (golos=false). Если просит «всегда отвечай голосом» или «всегда текстом», ставь vsegda=true.
  Сейчас по умолчанию: {fmt}.
Строки «[сделано: …]» в истории значат, что это уже сделано: не повторяй и не говори, что не сделал.
Правила: не выдумывай факты, цифры, результаты и сроки. Не обещай того, что команда не подтвердила.
Сейчас {now} МСК.

Последние новости команды (время МСК):
{recent}
"""

CHAT_TOOLS = [
    {"type": "web_search_20260209", "name": "web_search", "max_uses": 5},
    {"name": "peredat_komande",
     "description": "Передать поручение Арза команде агентов Claude. Они возьмут его в работу и сообщат результат.",
     "input_schema": {"type": "object", "properties": {
         "zadacha": {"type": "string", "description": "Поручение полностью, по-русски, со всеми деталями"}},
         "required": ["zadacha"]}},
    {"name": "format_otveta",
     "description": "Выбрать, как отправить ответ: голосовым сообщением или текстом.",
     "input_schema": {"type": "object", "properties": {
         "golos": {"type": "boolean", "description": "true = ответить голосовым"},
         "vsegda": {"type": "boolean", "description": "true = запомнить и так отвечать всегда"}},
         "required": ["golos"]}},
    {"name": "tihie_chasy",
     "description": "Поменять часы (МСК), когда сервис сам не звонит Арзу, а пишет текстом. Его звонки принимаем всегда.",
     "input_schema": {"type": "object", "properties": {
         "s": {"type": "integer", "description": "С какого часа не звонить, 0-23"},
         "do": {"type": "integer", "description": "До какого часа, 0-23"},
         "vykl": {"type": "boolean", "description": "true = звонить в любое время"}}}},
    {"name": "pozvonit",
     "description": "Позвонить Арзу голосом в Telegram: сейчас (minuty=0) или через N минут.",
     "input_schema": {"type": "object", "properties": {
         "minuty": {"type": "number"},
         "zachem": {"type": "string", "description": "О чём поговорить, коротко"}},
         "required": ["minuty"]}},
]


def pcm_to_ogg(pcm: bytes, rate=24000) -> bytes:
    """Сырые 16-бит моно → OGG Opus (голосовое Telegram). Без ffmpeg, через PyAV."""
    import io
    import av
    buf = io.BytesIO()
    out = av.open(buf, "w", format="ogg")
    st = out.add_stream("libopus", rate=48000)
    st.layout = "mono"
    res = av.AudioResampler(format="s16", layout="mono", rate=48000)
    fr = av.AudioFrame.from_ndarray(np.frombuffer(pcm, dtype=np.int16).reshape(1, -1), format="s16", layout="mono")
    fr.sample_rate = rate
    for f in res.resample(fr):
        for p in st.encode(f):
            out.mux(p)
    for f in res.resample(None) or []:
        for p in st.encode(f):
            out.mux(p)
    for p in st.encode(None):
        out.mux(p)
    out.close()
    return buf.getvalue()


async def speech_to_text(data: bytes, mime: str) -> str:
    last = None
    for m in STT_MODELS:
        try:
            r = await gem.aio.models.generate_content(model=m.strip(), contents=[
                gt.Part.from_bytes(data=data, mime_type=mime or "audio/ogg"),
                "Распознай речь дословно (русский или азербайджанский). Верни только текст, без пояснений."])
            if r.text and r.text.strip():
                return r.text.strip()
        except Exception as e:
            last = e
            log.warning("STT %s: %s", m, e)
    raise RuntimeError(f"не распознал голос: {last}")


async def text_to_voice(text: str) -> bytes:
    last = None
    for m in TTS_MODELS:
        try:
            r = await gem.aio.models.generate_content(
                model=m.strip(), contents=f"Скажи спокойно и дружелюбно, по-русски: {text}",
                config=gt.GenerateContentConfig(
                    response_modalities=["AUDIO"],
                    speech_config=gt.SpeechConfig(voice_config=gt.VoiceConfig(
                        prebuilt_voice_config=gt.PrebuiltVoiceConfig(voice_name=VOICE)))))
            part = r.candidates[0].content.parts[0].inline_data
            rate = 24000
            if part.mime_type and "rate=" in part.mime_type:
                rate = int(part.mime_type.split("rate=")[1].split(";")[0])
            return await asyncio.to_thread(pcm_to_ogg, part.data, rate)
        except Exception as e:
            last = e
            log.warning("TTS %s: %s", m, e)
    raise RuntimeError(f"не озвучил: {last}")


def chat_history():
    return state.setdefault("chat", [])


def chat_remember(role, text):
    h = chat_history()
    h.append({"r": role, "x": text[:2000], "t": now_msk().strftime("%d.%m %H:%M")})
    state["chat"] = h[-16:]
    save_state(state)


chat_done = []  # что сделал помощник за этот ответ (чтобы помнил и не повторял)
chat_fmt = {"voice": None}  # выбор формата на этот ответ (None = как спросили)


async def run_chat_tool(name, args):
    global callback_task
    if name == "peredat_komande":
        z = (args.get("zadacha") or "").strip()
        await post_queue(f"📌 Задача от Арза из чата: {z}")
        chat_done.append(f"передал команде: {z[:300]}")
        return "передано команде, они возьмут в работу и сообщат результат"
    if name == "format_otveta":
        v = bool(args.get("golos"))
        chat_fmt["voice"] = v
        if args.get("vsegda"):
            state["voice_pref"] = v
            save_state(state)
            chat_done.append("теперь всегда отвечаю " + ("голосом" if v else "текстом"))
        return "ответ уйдёт " + ("голосовым" if v else "текстом") + ", просто напиши сам ответ"
    if name == "tihie_chasy":
        if args.get("vykl"):
            state["quiet"] = "off"
        else:
            state["quiet"] = f"{int(args.get('s', 22))}-{int(args.get('do', 10))}"
        save_state(state)
        chat_done.append(f"тихие часы: {state['quiet']}")
        return f"готово, тихие часы {state['quiet']} (МСК)"
    if name == "pozvonit":
        mins = float(args.get("minuty") or 0)
        why = args.get("zachem", "")
        if mins <= 0:
            await notice_q.put({"callback": True, "force": True,
                                "text": f"Арз в чате попросил позвонить. {why}".strip()})
            chat_done.append("поставил звонок сейчас")
            return "звоню сейчас"
        if callback_task and not callback_task.done():
            callback_task.cancel()
        callback_task = asyncio.create_task(delayed_callback(mins, why))
        await post_queue(f"Перезвоню Арзу через {mins:g} мин (просьба из чата). {why}")
        chat_done.append(f"поставил звонок через {mins:g} мин")
        return f"позвоню через {mins:g} мин"
    return "нет такого инструмента"


async def chat_answer(content) -> str:
    """content: блоки сообщения Арза для Claude. Возвращает текст ответа."""
    recent = "\n".join(f"{r['t']}: {r['x']}" for r in state["recent"][-15:]) or "пока нет"
    msgs = []
    for h in chat_history()[-12:]:
        role = "user" if h["r"] == "arz" else "assistant"
        if msgs and msgs[-1]["role"] == role:
            msgs[-1]["content"] += "\n" + h["x"]
        else:
            msgs.append({"role": role, "content": h["x"]})
    if msgs and msgs[0]["role"] == "assistant":
        msgs = msgs[1:]
    if msgs and msgs[-1]["role"] == "user":  # последнее сообщение Арза пришлём ниже с картинками
        msgs.pop()
    msgs.append({"role": "user", "content": content})
    pref = state.get("voice_pref")
    fmt = ("всегда голосом" if pref else "всегда текстом") if pref is not None else \
        "на голосовое голосом, на текст текстом"
    sysp = CHAT_SYSTEM.format(now=now_msk().strftime("%d.%m %H:%M"), recent=recent, fmt=fmt,
                              quiet=state.get("quiet", QUIET_HOURS))
    r = None
    for _ in range(6):
        r = await claude.messages.create(model=CHAT_MODEL, max_tokens=3000, system=sysp,
                                         output_config={"effort": "medium"}, tools=CHAT_TOOLS, messages=msgs)
        if r.stop_reason == "pause_turn":
            msgs.append({"role": "assistant", "content": r.content})
            continue
        if r.stop_reason == "tool_use":
            msgs.append({"role": "assistant", "content": r.content})
            results = []
            for b in r.content:
                if b.type == "tool_use":
                    try:
                        out = await run_chat_tool(b.name, b.input or {})
                    except Exception as e:
                        out = f"ошибка: {e}"
                    results.append({"type": "tool_result", "tool_use_id": b.id, "content": out})
            msgs.append({"role": "user", "content": results})
            continue
        break
    text = "".join(b.text for b in r.content if b.type == "text") if r else ""
    return text.replace("**", "").replace("*", "").strip() or "Понял."


async def handle_chat(batch):
    """batch: несколько сообщений Арза подряд. Голос → отвечаем голосом, текст → текстом."""
    voice_in = any(m.voice or m.video_note for m in batch)
    parts, plain = [], []
    for m in batch:
        try:
            if m.voice or m.video_note or (m.audio and not m.message):
                data = await m.download_media(file=bytes)
                mime = "video/mp4" if m.video_note else (m.file.mime_type or "audio/ogg")
                said = await speech_to_text(data, mime)
                plain.append(f"(голосом) {said}")
            elif m.photo or (m.document and (m.file.mime_type or "").startswith("image/")):
                import base64
                data = await m.download_media(file=bytes)
                mime = m.file.mime_type if m.document else "image/jpeg"
                parts.append({"type": "image", "source": {"type": "base64", "media_type": mime,
                                                          "data": base64.b64encode(data).decode()}})
                plain.append(f"(картинка) {m.message or ''}".strip())
            elif m.message:
                plain.append(m.message)
            elif m.document:
                plain.append(f"(прислал файл {m.file.name or ''}, читать файлы в чате пока не умею)")
        except Exception as e:
            log.exception("чат: не смог разобрать сообщение")
            plain.append(f"(сообщение не разобрал: {type(e).__name__})")
    said = "\n".join(p for p in plain if p)
    if not said and not parts:
        return
    chat_remember("arz", said)
    chat_done.clear()
    chat_fmt["voice"] = None
    log.info("чат от Арза: %s", said[:120])
    try:
        async with tg.action(ARZ_ID, "record-voice" if voice_in else "typing"):
            answer = await chat_answer(parts + [{"type": "text", "text": said or "(без текста)"}])
            ogg = None
            pref = state.get("voice_pref")
            if chat_fmt["voice"] is not None:
                voice_in = chat_fmt["voice"]
            elif pref is not None:
                voice_in = pref
            if voice_in:
                try:
                    ogg = await text_to_voice(answer)
                except Exception as e:
                    log.warning("голосом не вышло, отвечу текстом: %s", e)
    except Exception as e:
        log.exception("чат: ответ")
        answer, ogg = f"Не получилось ответить сейчас ({type(e).__name__}). Передал команде.", None
        await post_queue(f"📌 Задача от Арза из чата (ответ не сработал, ошибка {type(e).__name__}): {said}")
    chat_remember("me", answer + "".join(f"\n[сделано: {d}]" for d in chat_done))
    if ogg:
        import io
        f = io.BytesIO(ogg)
        f.name = "otvet.ogg"
        await tg.send_file(ARZ_ID, f, voice_note=True)
    else:
        await text_arz(answer)
    await post_queue(f"Чат. Арз: {said[:1500]}\nОтвет: {answer[:1500]}")


async def chat_worker():
    while True:
        batch = [await chat_q.get()]
        await asyncio.sleep(CHAT_WAIT)
        while not chat_q.empty():
            batch.append(chat_q.get_nowait())
        try:
            await handle_chat(batch)
        except Exception:
            log.exception("chat_worker")


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

    @tg.on(events.NewMessage(chats=ARZ_ID, incoming=True))
    async def _from_arz(ev):
        await chat_q.put(ev.message)

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
    await asyncio.gather(worker(), poll_queue(), chat_worker(), tg.run_until_disconnected())


if __name__ == "__main__":
    # PyTgCalls берёт цикл событий при создании, поэтому запускаем main на нём же
    calls.loop.run_until_complete(main())

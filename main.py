import os
import base64
import logging
import asyncio

import httpx
from dotenv import load_dotenv
from fastapi import FastAPI, Request

load_dotenv()

# ── Config ──────────────────────────────────────────────────────────────────
BOT_NAME = os.getenv("BOT_NAME", "EducaBot")
COMPANY_NAME = os.getenv("COMPANY_NAME", "EducaPro")
COMPANY_DESC = os.getenv(
    "COMPANY_DESC",
    "Vende licencias de Canva y de Windows a 10 USD cada una.",
)
BOT_TONE = os.getenv("BOT_TONE", "Claro, breve, amable, en español neutro.")
BOT_LANG = os.getenv("BOT_LANG", "Español neutro.")

CHATWOOT_URL = os.getenv("CHATWOOT_URL")
CHATWOOT_API_TOKEN = os.getenv("CHATWOOT_API_TOKEN")
CHATWOOT_ACCOUNT_ID = os.getenv("CHATWOOT_ACCOUNT_ID")
OPENROUTER_API_KEY = os.getenv("OPENROUTER_API_KEY")
OPENROUTER_MODEL = os.getenv("OPENROUTER_MODEL", "google/gemini-2.5-flash")
PAUSE_LABEL = os.getenv("PAUSE_LABEL", "bot_off")
MAX_HISTORIAL = int(os.getenv("MAX_HISTORIAL", "20"))
PORT = int(os.getenv("PORT", "8000"))

SYSTEM_PROMPT = (
    f"Eres {BOT_NAME}, el asistente virtual de {COMPANY_NAME}. "
    f"{COMPANY_NAME} {COMPANY_DESC}\n\n"
    f"Tono: {BOT_TONE}\n"
    f"Idioma: {BOT_LANG}\n\n"
    "Reglas:\n"
    "- Responde de forma clara, breve y amable.\n"
    "- No inventes precios ni información que no tengas.\n"
    "- Si no sabes algo, ofrece pasar a un asesor humano.\n"
    "- No menciones que eres una IA ni detalles técnicos.\n"
    "- Sé empático y profesional."
)

# ── Logging ──────────────────────────────────────────────────────────────────
logger = logging.getLogger(__name__)

# ── App ─────────────────────────────────────────────────────────────────────
app = FastAPI(title=BOT_NAME)


@app.on_event("startup")
async def _on_startup():
    logging.basicConfig(
        level=logging.INFO,
        format="%(asctime)s [%(levelname)s] %(message)s",
        force=True,
        stream=__import__("sys").stdout,
    )
    logger.info("=" * 50)
    logger.info("Bot iniciado: %s / %s", BOT_NAME, COMPANY_NAME)
    logger.info("Chatwoot URL: %s", CHATWOOT_URL)
    logger.info("Modelo: %s | MAX_HISTORIAL: %s | PAUSE_LABEL: %s", OPENROUTER_MODEL, MAX_HISTORIAL, PAUSE_LABEL)
    logger.info("=" * 50)


# ── Helpers ─────────────────────────────────────────────────────────────────
def ensure_absolute(url: str) -> str:
    if url.startswith("//"):
        return "https:" + url
    if url.startswith("/"):
        base = CHATWOOT_URL.rstrip("/")
        return base + url
    return url


async def download_file(url: str) -> tuple[bytes, str | None] | None:
    url = ensure_absolute(url)
    headers = {"api_access_token": CHATWOOT_API_TOKEN}
    try:
        async with httpx.AsyncClient(follow_redirects=True, timeout=60) as c:
            r = await c.get(url, headers=headers)
            r.raise_for_status()
            ct = r.headers.get("content-type")
            return r.content, ct
    except Exception as e:
        logger.error("Error descargando %s: %s", url, e)
        return None


async def convert_to_mp3(data: bytes) -> tuple[bytes, str]:
    try:
        proc = await asyncio.create_subprocess_exec(
            "ffmpeg",
            "-hide_banner",
            "-loglevel",
            "error",
            "-i",
            "pipe:0",
            "-f",
            "mp3",
            "-ac",
            "1",
            "-ar",
            "16000",
            "pipe:1",
            stdin=asyncio.subprocess.PIPE,
            stdout=asyncio.subprocess.PIPE,
            stderr=asyncio.subprocess.PIPE,
        )
        stdout, stderr = await asyncio.wait_for(
            proc.communicate(data), timeout=30
        )
        if proc.returncode == 0 and len(stdout) > 0:
            logger.info(
                "Audio convertido a MP3: %d -> %d bytes", len(data), len(stdout)
            )
            return stdout, "mp3"
        logger.warning("ffmpeg falló (rc=%s): %s", proc.returncode, stderr.decode())
    except FileNotFoundError:
        logger.warning("ffmpeg no instalado, usando formato original")
    except Exception as e:
        logger.warning("Error convirtiendo audio: %s", e)
    return data, "ogg"


async def _fetch_labels(conversation_id: int) -> list:
    url = (
        f"{CHATWOOT_URL}/api/v1/accounts/{CHATWOOT_ACCOUNT_ID}"
        f"/conversations/{conversation_id}/labels"
    )
    headers = {"api_access_token": CHATWOOT_API_TOKEN}
    try:
        async with httpx.AsyncClient(timeout=60) as c:
            r = await c.get(url, headers=headers)
            if r.status_code == 200:
                return r.json().get("labels", [])
            logger.warning("Labels API: %s %s", r.status_code, r.text[:200])
    except Exception as e:
        logger.warning("Error obteniendo labels: %s", e)
    return []


def _get_labels_from_payload(payload: dict) -> list | None:
    labels = payload.get("conversation", {}).get("labels")
    return labels if labels is not None else None


async def _fetch_history(conversation_id: int, exclude_id: int) -> list:
    url = (
        f"{CHATWOOT_URL}/api/v1/accounts/{CHATWOOT_ACCOUNT_ID}"
        f"/conversations/{conversation_id}/messages"
    )
    headers = {"api_access_token": CHATWOOT_API_TOKEN}
    try:
        async with httpx.AsyncClient(timeout=60) as c:
            r = await c.get(url, headers=headers)
            if r.status_code != 200:
                return []
            data = r.json()
            if isinstance(data, dict):
                data = data.get("data") or data.get("payload") or []
            history = []
            for m in data:
                if m.get("id") == exclude_id:
                    continue
                t = m.get("message_type")
                if t == 0:
                    role = "user"
                elif t == 1:
                    role = "assistant"
                else:
                    continue
                text = (m.get("content") or "").strip()
                if not text:
                    continue
                history.append({"role": role, "content": text})
            return history[-MAX_HISTORIAL:]
    except Exception as e:
        logger.warning("Error obteniendo historial: %s", e)
        return []


async def _build_content(text: str, attachments: list) -> list:
    parts = []
    if text:
        parts.append({"type": "text", "text": text})

    for att in attachments or []:
        url = att.get("download_url") or att.get("data_url") or ""
        if not url:
            continue

        ft = att.get("file_type")

        if ft == "image":
            result = await download_file(url)
            if result:
                data, _ = result
                b64 = base64.b64encode(data).decode()
                parts.append(
                    {
                        "type": "text",
                        "text": "[El cliente envió una imagen. Analízala y responde a lo que pide.]",
                    }
                )
                parts.append(
                    {
                        "type": "image_url",
                        "image_url": {"url": f"data:image/jpeg;base64,{b64}"},
                    }
                )
            else:
                parts.append(
                    {
                        "type": "text",
                        "text": "[El cliente envió una imagen que no pudo descargarse. Pídele que la reenvíe.]",
                    }
                )

        elif ft == "audio":
            result = await download_file(url)
            if result:
                data, content_type = result
                logger.info("Audio descargado: %d bytes, content-type: %s", len(data), content_type)
                audio_data, fmt = await convert_to_mp3(data)
                b64 = base64.b64encode(audio_data).decode()
                parts.append(
                    {
                        "type": "text",
                        "text": (
                            f"El cliente envió una nota de voz. Escúchala y responde "
                            f"a lo que pide como {BOT_NAME}, en español neutro. "
                            f"Haz tu mejor esfuerzo por entender lo que dice "
                            f"aunque el audio no sea perfecto."
                        ),
                    }
                )
                parts.append(
                    {
                        "type": "input_audio",
                        "input_audio": {"data": b64, "format": fmt},
                    }
                )
            else:
                parts.append(
                    {
                        "type": "text",
                        "text": "[El cliente envió una nota de voz que no pudo descargarse. Pídele que escriba su consulta.]",
                    }
                )

        else:
            parts.append(
                {
                    "type": "text",
                    "text": f"[El cliente adjuntó un archivo de tipo {ft}. Pídele más detalles por texto si es necesario.]",
                }
            )

    return parts


async def _call_llm(messages: list) -> str | None:
    headers = {
        "Authorization": f"Bearer {OPENROUTER_API_KEY}",
        "Content-Type": "application/json",
        "HTTP-Referer": CHATWOOT_URL or "http://localhost",
    }
    payload = {
        "model": OPENROUTER_MODEL,
        "messages": messages,
        "max_tokens": 1024,
    }
    try:
        async with httpx.AsyncClient(timeout=120) as c:
            r = await c.post(
                "https://openrouter.ai/api/v1/chat/completions",
                headers=headers,
                json=payload,
            )
            if r.status_code >= 400:
                logger.error("OpenRouter error %s: %s", r.status_code, r.text[:500])
                return None
            data = r.json()
            return data.get("choices", [{}])[0].get("message", {}).get("content")
    except Exception as e:
        logger.error("Error llamando a OpenRouter: %s", e)
        return None


async def _send_to_chatwoot(conversation_id: int, content: str):
    url = (
        f"{CHATWOOT_URL}/api/v1/accounts/{CHATWOOT_ACCOUNT_ID}"
        f"/conversations/{conversation_id}/messages"
    )
    headers = {"api_access_token": CHATWOOT_API_TOKEN}
    payload = {"content": content, "message_type": "outgoing"}
    try:
        async with httpx.AsyncClient(timeout=60) as c:
            r = await c.post(url, headers=headers, json=payload)
            if r.status_code >= 400:
                logger.error(
                    "Chatwoot send error %s: %s", r.status_code, r.text[:300]
                )
            else:
                logger.info("Respuesta enviada a conversación %s", conversation_id)
    except Exception as e:
        logger.error("Error enviando a Chatwoot: %s", e)


# ── Endpoints ───────────────────────────────────────────────────────────────
@app.get("/")
async def health():
    return {
        "status": "ok",
        "bot": BOT_NAME,
        "company": COMPANY_NAME,
        "model": OPENROUTER_MODEL,
    }


@app.post("/webhook")
async def webhook(request: Request):
    raw = await request.body()
    logger.info("WEBHOOK RECIBIDO: %s", raw[:500])
    try:
        payload = __import__("json").loads(raw)
    except Exception:
        logger.error("JSON inválido: %s", raw[:300])
        return {"status": "ignored", "reason": "invalid json"}

    event = payload.get("event")
    msg = payload.get("message") or payload
    message_type = msg.get("message_type")
    logger.info("Evento: %s | message_type: %s", event, message_type)

    if event != "message_created":
        logger.info("Ignorado — evento %s", event)
        return {"status": "ignored", "reason": f"event={event}"}

    is_incoming = (
        message_type == "incoming"
        or message_type == 0
        or message_type == "0"
    )
    if not is_incoming:
        logger.info("Ignorado — message_type=%s no es incoming", message_type)
        return {"status": "ignored", "reason": f"not incoming ({message_type})"}

    conversation_id = (
        payload.get("conversation", {}).get("id")
        or payload.get("conversation_id")
        or msg.get("conversation_id")
    )
    if not conversation_id:
        logger.warning("No conversation_id en payload")
        return {"status": "ignored", "reason": "no conversation_id"}

    # ── Pause label ─────────────────────────────────────────────────────
    labels = _get_labels_from_payload(payload)
    if labels is None:
        labels = await _fetch_labels(conversation_id)
    if PAUSE_LABEL in labels:
        logger.info("Conversación %s tiene label '%s' — ignorando", conversation_id, PAUSE_LABEL)
        return {"status": "paused"}

    text = msg.get("content", "") or ""
    attachments = msg.get("attachments", [])
    message_id = msg.get("id")

    logger.info(
        "Mensaje %s en conv %s: text=%s attachments=%d",
        message_id,
        conversation_id,
        text[:120],
        len(attachments),
    )
    for att in attachments:
        logger.info(
            "  Attachment: type=%s url=%s",
            att.get("file_type"),
            att.get("download_url") or att.get("data_url"),
        )

    # ── Memoria ──────────────────────────────────────────────────────────
    history = await _fetch_history(conversation_id, message_id)
    logger.info("Historial: %d mensajes previos", len(history))

    # ── Content multimodal ───────────────────────────────────────────────
    content_parts = await _build_content(text, attachments)
    if not content_parts:
        content_parts = [{"type": "text", "text": "(mensaje vacío)"}]

    # ── Llamada al LLM ───────────────────────────────────────────────────
    llm_messages = [{"role": "system", "content": SYSTEM_PROMPT}]
    llm_messages.extend(history)
    llm_messages.append({"role": "user", "content": content_parts})

    response = await _call_llm(llm_messages)
    if not response:
        response = (
            "Lo siento, no pude procesar tu mensaje en este momento. "
            "Un asesor te atenderá pronto."
        )

    logger.info("Respuesta: %s", response[:250])

    # ── Enviar a Chatwoot ────────────────────────────────────────────────
    await _send_to_chatwoot(conversation_id, response)

    return {"status": "ok"}

"""
常驻 Broker：跨 MCP 工具调用复用同一个窗口和 WebSocket 连接。
负责：
  1. 提供前端页面与静态资源
  2. 接收用户输入 → 存进队列
  3. 把 LLM 的回复拆块、按速率流式推送给前端
  4. 代理用户配置的 ASR 服务（音频转写，含 ffmpeg 转码）
  5. 代理用户配置的 TTS 服务（文本转语音）
  6. 用户点"结束"或关闭窗口时，通知等待中的工具
"""

import asyncio
import json
import os
import shutil
import subprocess
import sys
import tempfile
from pathlib import Path

import httpx
from aiohttp import web

STATIC_DIR = Path(__file__).resolve().parent / "static"


def log(msg: str) -> None:
    print(f"[Broker] {msg}", file=sys.stderr, flush=True)


def _normalize_asr_url(url: str) -> str:
    """把用户填写的 ASR URL 规范化到完整的 /audio/transcriptions 端点。"""
    u = (url or "").strip().rstrip("/")
    if not u:
        return ""
    if u.endswith("/audio/transcriptions"):
        return u
    if u.endswith("/v1"):
        return u + "/audio/transcriptions"
    return u + "/v1/audio/transcriptions"


def _normalize_tts_url(url: str) -> str:
    """把用户填写的 TTS URL 规范化到完整的 /audio/speech 端点。"""
    u = (url or "").strip().rstrip("/")
    if not u:
        return ""
    if u.endswith("/audio/speech"):
        return u
    if u.endswith("/v1"):
        return u + "/audio/speech"
    return u + "/v1/audio/speech"


def _extract_asr_text(result) -> str:
    """从 ASR 响应中提取纯文本，兼容多种服务商格式。"""
    if not isinstance(result, dict):
        return ""
    text = result.get("text", "") or ""
    if not text:
        data = result.get("data")
        if isinstance(data, dict):
            text = data.get("text", "") or ""
    if not text:
        return ""
    if "<asr_text>" in text:
        text = text.split("<asr_text>", 1)[1]
        if "</asr_text>" in text:
            text = text.split("</asr_text>", 1)[0]
    return text.strip()


def _find_ffmpeg() -> str:
    """定位 ffmpeg 可执行文件，优先 PATH，其次常见位置。"""
    for name in ("ffmpeg", "ffmpeg.exe"):
        p = shutil.which(name)
        if p:
            return p
    for cand in (
        r"C:\ffmpeg\bin\ffmpeg.exe",
        r"C:\Program Files\ffmpeg\bin\ffmpeg.exe",
    ):
        if os.path.exists(cand):
            return cand
    return ""


def _transcode_to_wav(audio_bytes: bytes, src_ext: str) -> bytes:
    """用 ffmpeg 把任意音频转成 16kHz 单声道 WAV。"""
    ffmpeg = _find_ffmpeg()
    if not ffmpeg:
        raise RuntimeError("未找到 ffmpeg，请安装并加入 PATH（参考 https://www.gyan.dev/ffmpeg/builds/）")

    src_fd, src_path = tempfile.mkstemp(suffix="." + src_ext)
    wav_fd, wav_path = tempfile.mkstemp(suffix=".wav")
    os.close(wav_fd)
    try:
        with os.fdopen(src_fd, "wb") as f:
            f.write(audio_bytes)
        proc = subprocess.run(
            [ffmpeg, "-y", "-i", src_path, "-ar", "16000", "-ac", "1", "-f", "wav", wav_path],
            capture_output=True, text=True, timeout=60,
        )
        if proc.returncode != 0:
            raise RuntimeError(f"ffmpeg 转码失败: {(proc.stderr or '')[-500:]}")
        with open(wav_path, "rb") as f:
            return f.read()
    finally:
        for p in (src_path, wav_path):
            try:
                os.unlink(p)
            except OSError:
                pass


class BrokerSession:
    RATE_MIN = 1
    RATE_MAX = 60
    RATE_DEFAULT = 5

    def __init__(self, session_id: str, topic: str):
        self.session_id = session_id
        self.topic = topic
        self.websocket = None
        self.runner = None
        self.port = None
        self.user_queue = asyncio.Queue()
        self.ended = False
        self.stream_rate = self.RATE_DEFAULT
        self.asr_config = {"url": "", "model": "", "api_key": ""}
        self.tts_config = {"url": "", "model": "", "voice": "", "api_key": ""}

    async def start(self) -> str:
        app = web.Application()
        app.router.add_get("/", self.handle_index)
        app.router.add_get("/ws", self.handle_ws)
        app.router.add_post("/transcribe", self.handle_transcribe)
        app.router.add_post("/tts", self.handle_tts)
        app.router.add_static("/static/", STATIC_DIR)
        self.runner = web.AppRunner(app)
        await self.runner.setup()
        site = web.TCPSite(self.runner, "127.0.0.1", 0)
        await site.start()
        self.port = self.runner.addresses[0][1]
        url = f"http://127.0.0.1:{self.port}/?session={self.session_id}"
        log(f"listening on {url}")
        return url

    async def stop(self) -> None:
        if self.websocket and not self.websocket.closed:
            try:
                await self.websocket.close()
            except Exception:
                pass
        if self.runner:
            try:
                await asyncio.wait_for(self.runner.cleanup(), timeout=3.0)
            except asyncio.TimeoutError:
                log("cleanup timeout")

    async def handle_index(self, request: web.Request) -> web.StreamResponse:
        return web.FileResponse(STATIC_DIR / "index.html")

    async def handle_ws(self, request: web.Request) -> web.StreamResponse:
        ws = web.WebSocketResponse()
        await ws.prepare(request)
        self.websocket = ws
        log("ws connected")
        await ws.send_json({
            "type": "init",
            "session_id": self.session_id,
            "topic": self.topic,
            "stream_rate": self.stream_rate,
        })
        try:
            async for msg in ws:
                if msg.type == web.WSMsgType.TEXT:
                    try:
                        data = json.loads(msg.data)
                    except json.JSONDecodeError:
                        continue
                    await self._handle(data)
                elif msg.type == web.WSMsgType.ERROR:
                    break
        finally:
            log("ws disconnected")
            if not self.ended:
                self.ended = True
                await self.user_queue.put({"__ended__": True})
        return ws

    async def _handle(self, data: dict) -> None:
        mtype = data.get("type")
        if mtype == "user_message":
            content = (data.get("content") or "").strip()
            if content:
                log(f"user: {content!r}")
                await self.user_queue.put({"content": content})
        elif mtype == "end_session":
            log("user ended session")
            if not self.ended:
                self.ended = True
                await self.user_queue.put({"__ended__": True})
        elif mtype == "set_rate":
            try:
                r = int(data.get("rate", self.RATE_DEFAULT))
                self.stream_rate = max(self.RATE_MIN, min(self.RATE_MAX, r))
                log(f"stream_rate set to {self.stream_rate}")
            except (TypeError, ValueError):
                pass
        elif mtype == "set_asr_config":
            self.asr_config = {
                "url": (data.get("url") or "").strip(),
                "model": (data.get("model") or "").strip(),
                "api_key": (data.get("api_key") or "").strip(),
            }
            log(f"asr_config updated: url={self.asr_config['url'][:40]} model={self.asr_config['model']}")
        elif mtype == "set_tts_config":
            self.tts_config = {
                "url": (data.get("url") or "").strip(),
                "model": (data.get("model") or "").strip(),
                "voice": (data.get("voice") or "").strip(),
                "api_key": (data.get("api_key") or "").strip(),
            }
            log(f"tts_config updated: url={self.tts_config['url'][:40]} model={self.tts_config['model']} voice={self.tts_config['voice']}")

    # ------------------------------------------------------------------ #
    # 音频转写代理
    # ------------------------------------------------------------------ #
    async def handle_transcribe(self, request: web.Request) -> web.StreamResponse:
        cfg = self.asr_config
        url = _normalize_asr_url(cfg.get("url", ""))
        if not url:
            return web.json_response(
                {"error": "尚未配置 ASR 服务，请点右上角 ⚙️ 填写语音识别服务的 URL"},
                status=400,
            )

        try:
            form = await request.post()
        except Exception as e:
            return web.json_response({"error": f"读取上传失败: {e}"}, status=400)

        file_field = form.get("file")
        if not file_field or not hasattr(file_field, "file"):
            return web.json_response({"error": "缺少音频文件"}, status=400)

        audio_bytes = file_field.file.read()
        filename = file_field.filename or "audio.webm"
        src_ext = filename.rsplit(".", 1)[-1].lower() if "." in filename else "webm"

        try:
            wav_bytes = await asyncio.to_thread(_transcode_to_wav, audio_bytes, src_ext)
        except Exception as e:
            log(f"转码失败: {e}")
            return web.json_response({"error": f"音频转码失败: {e}"}, status=500)

        headers = {}
        if cfg.get("api_key"):
            headers["Authorization"] = f"Bearer {cfg['api_key']}"

        files = {"file": ("audio.wav", wav_bytes, "audio/wav")}
        data = {}
        if cfg.get("model"):
            data["model"] = cfg["model"]

        log(f"transcribe → {url} ({len(wav_bytes)} bytes wav, src={src_ext})")
        try:
            async with httpx.AsyncClient(timeout=120) as client:
                resp = await client.post(url, files=files, data=data, headers=headers)
        except Exception as e:
            return web.json_response({"error": f"请求 ASR 服务失败: {e}"}, status=502)

        if resp.status_code != 200:
            return web.json_response(
                {"error": f"ASR 服务返回 {resp.status_code}: {resp.text[:300]}"},
                status=502,
            )

        try:
            result = resp.json()
        except Exception:
            return web.json_response({"error": f"ASR 响应非 JSON: {resp.text[:200]}"}, status=502)

        text = _extract_asr_text(result)
        log(f"transcribe ← {text[:60]!r}")
        return web.json_response({"text": text})

    # ------------------------------------------------------------------ #
    # TTS 代理
    # ------------------------------------------------------------------ #
    async def handle_tts(self, request: web.Request) -> web.StreamResponse:
        cfg = self.tts_config
        url = _normalize_tts_url(cfg.get("url", ""))
        if not url:
            return web.json_response({"error": "未配置 TTS 服务"}, status=400)

        try:
            body = await request.json()
        except Exception:
            return web.json_response({"error": "请求体不是 JSON"}, status=400)

        text = (body.get("text") or "").strip()
        if not text:
            return web.json_response({"error": "text 为空"}, status=400)

        # voice 允许前端逐次覆盖（如切换角色），否则用设置里的默认值
        voice = (body.get("voice") or cfg.get("voice") or "").strip()

        headers = {"Content-Type": "application/json"}
        if cfg.get("api_key"):
            headers["Authorization"] = f"Bearer {cfg['api_key']}"

        payload = {
            "model": cfg.get("model") or "tts-1",
            "input": text,
            "response_format": "mp3",
        }
        if voice:
            payload["voice"] = voice

        log(f"tts → {url} ({len(text)} chars, voice={voice!r})")
        try:
            async with httpx.AsyncClient(timeout=120) as client:
                resp = await client.post(url, json=payload, headers=headers)
        except Exception as e:
            return web.json_response({"error": f"请求 TTS 服务失败: {e}"}, status=502)

        if resp.status_code != 200:
            return web.json_response(
                {"error": f"TTS 服务返回 {resp.status_code}: {resp.text[:300]}"},
                status=502,
            )

        # 直接透传音频字节；content-type 优先用服务端返回的
        ct = resp.headers.get("content-type", "audio/mpeg")
        log(f"tts ← {len(resp.content)} bytes ({ct})")
        return web.Response(body=resp.content, content_type=ct.split(";")[0])

    # ------------------------------------------------------------------ #
    # 流式推送
    # ------------------------------------------------------------------ #
    @staticmethod
    def _split_chunks(content: str, target_size: int = 24) -> list[str]:
        chunks = []
        i = 0
        n = len(content)
        while i < n:
            j = min(i + target_size, n)
            if j < n:
                lt = content.rfind('<', i, j)
                if lt != -1:
                    gt = content.find('>', lt)
                    if gt != -1 and gt - j < 40:
                        j = gt + 1
            chunks.append(content[i:j])
            i = j
        return chunks

    async def push_reply(self, content: str) -> None:
        if not self.websocket or self.websocket.closed:
            return

        rate = self.stream_rate
        if rate <= 0:
            try:
                await self.websocket.send_json({
                    "type": "assistant_delta",
                    "content": content,
                })
                await self.websocket.send_json({"type": "assistant_done"})
            except Exception:
                pass
            return

        chunks = self._split_chunks(content)
        interval = 1.0 / rate

        for idx, chunk in enumerate(chunks):
            try:
                await self.websocket.send_json({
                    "type": "assistant_delta",
                    "content": chunk,
                })
            except Exception:
                return
            if idx < len(chunks) - 1:
                await asyncio.sleep(interval)

        try:
            await self.websocket.send_json({"type": "assistant_done"})
        except Exception:
            pass

    async def wait_for_user(self):
        if self.ended:
            return None
        item = await self.user_queue.get()
        if item.get("__ended__"):
            return None
        return item.get("content")
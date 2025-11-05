import asyncio
import aiohttp
from aiohttp import web
import json
import gzip
import struct
import uuid
import logging
import os
import re
import base64
from datetime import datetime
from pathlib import Path

# 基础日志（全局）
logging.basicConfig(level=logging.INFO, format='%(asctime)s - %(levelname)s - %(message)s')
logger = logging.getLogger("asr_server")

# 常量与协议定义
DEFAULT_SAMPLE_RATE = 16000

class ProtocolVersion:
    V1 = 0b0001

class MessageType:
    CLIENT_FULL_REQUEST = 0b0001
    CLIENT_AUDIO_ONLY_REQUEST = 0b0010
    SERVER_FULL_RESPONSE = 0b1001
    SERVER_ERROR_RESPONSE = 0b1111

class MessageTypeSpecificFlags:
    NO_SEQUENCE = 0b0000
    POS_SEQUENCE = 0b0001
    NEG_SEQUENCE = 0b0010
    NEG_WITH_SEQUENCE = 0b0011

class SerializationType:
    NO_SERIALIZATION = 0b0000
    JSON = 0b0001

class CompressionType:
    GZIP = 0b0001

# 凭据读取（复用 sauc_websocket_demo.py 中的配置或环境变量）
def load_env_file():
    """
    从项目根目录加载 .env 并合并到环境变量（若存在）。
    - 跳过空行与注释；仅在当前环境未设置该键时注入值。
    """
    env_path = Path(__file__).parent / '.env'
    if env_path.exists():
        try:
            for line in env_path.read_text(encoding='utf-8').splitlines():
                line = line.strip()
                if not line or line.startswith('#'):
                    continue
                if '=' in line:
                    key, val = line.split('=', 1)
                    key = key.strip()
                    val = val.strip()
                    if key and (key not in os.environ or os.getenv(key) is None or os.getenv(key) == ""):
                        os.environ[key] = val
        except Exception:
            # 保持静默，避免影响服务启动
            pass

# 先加载 .env，再读取环境变量（不再依赖 demo 配置）
load_env_file()
APP_KEY = os.getenv("APP_KEY", "")
ACCESS_KEY = os.getenv("ACCESS_KEY", "")
DEFAULT_RESOURCE_ID = os.getenv("RESOURCE_ID", "volc.bigasr.sauc.duration")
DEFAULT_BACKEND_URL = os.getenv("BACKEND_URL", "wss://openspeech.bytedance.com/api/v3/sauc/bigmodel_async")

# 对话服务配置（可通过环境变量或查询参数覆盖）
DEFAULT_DIALOG_URL = os.getenv("DIALOG_URL", "http://0.0.0.0:8084/chat/stream")
DEFAULT_DIALOG_MODE = os.getenv("DIALOG_MODE", "sse")  # 可选：sse 或 json
DEFAULT_VOICE_ID = os.getenv("VOICE_ID", "demo-voice")
DEFAULT_SYSTEM_PROMPT = os.getenv("SYSTEM_PROMPT", "你是一个礼貌且简洁的助手。")

# 语音克隆 TTS 配置（默认关闭）
def _env_bool(name: str, default_false: bool = True):
    try:
        val = os.getenv(name, "false" if default_false else "true")
        return val.lower() in ("1", "true", "yes", "y")
    except Exception:
        return not default_false

DEFAULT_VOICE_CLONE_ENABLED = _env_bool("VOICE_CLONE_ENABLED", default_false=True)
DEFAULT_VOICE_CLONE_URL = os.getenv("VOICE_CLONE_URL", "")
DEFAULT_VOICE_CLONE_MODE = os.getenv("VOICE_CLONE_MODE", "stream")  # stream 或 segments_http（预留）
DEFAULT_VOICE_CLONE_VOICE_TYPE = os.getenv("VOICE_CLONE_VOICE_TYPE", "demo")
DEFAULT_VOICE_CLONE_SEND_FINAL_ONLY = _env_bool("VOICE_CLONE_SEND_FINAL_ONLY", default_false=True)
try:
    DEFAULT_VOICE_CLONE_SAMPLE_RATE = int(os.getenv("VOICE_CLONE_SAMPLE_RATE", "24000"))
except Exception:
    DEFAULT_VOICE_CLONE_SAMPLE_RATE = 24000
DEFAULT_VOICE_CLONE_SAVE_PATH = os.getenv("VOICE_CLONE_SAVE_PATH", "")
DEFAULT_VOICE_CLONE_SAVE_MODE = os.getenv("VOICE_CLONE_SAVE_MODE", "append")

# 结果文本提取
def extract_final_text(payload_msg):
    try:
        if not payload_msg:
            return ""
        if isinstance(payload_msg, dict):
            res = payload_msg.get("result")
            if isinstance(res, dict):
                text = res.get("text")
                if isinstance(text, str) and text:
                    return text
                utts = res.get("utterances")
                if isinstance(utts, list) and utts:
                    texts = [u.get("text") for u in utts if isinstance(u, dict) and u.get("text")]
                    if texts:
                        return " ".join(texts)
            text = payload_msg.get("text")
            if isinstance(text, str):
                return text
        return json.dumps(payload_msg, ensure_ascii=False)
    except Exception:
        return ""

# 请求头构造
class AsrRequestHeader:
    def __init__(self):
        self.message_type = MessageType.CLIENT_FULL_REQUEST
        self.message_type_specific_flags = MessageTypeSpecificFlags.POS_SEQUENCE
        self.serialization_type = SerializationType.JSON
        self.compression_type = CompressionType.GZIP
        self.reserved_data = bytes([0x00])

    def with_message_type(self, message_type):
        self.message_type = message_type
        return self

    def with_message_type_specific_flags(self, flags):
        self.message_type_specific_flags = flags
        return self

    def with_serialization_type(self, serialization_type):
        self.serialization_type = serialization_type
        return self

    def with_compression_type(self, compression_type):
        self.compression_type = compression_type
        return self

    def with_reserved_data(self, reserved_data):
        self.reserved_data = reserved_data
        return self

    def to_bytes(self):
        header = bytearray()
        header.append((ProtocolVersion.V1 << 4) | 1)
        header.append((self.message_type << 4) | self.message_type_specific_flags)
        header.append((self.serialization_type << 4) | self.compression_type)
        header.extend(self.reserved_data)
        return bytes(header)

# 响应解析
class AsrResponse:
    def __init__(self):
        self.code = 0
        self.event = 0
        self.is_last_package = False
        self.payload_sequence = 0
        self.payload_size = 0
        self.payload_msg = None

    def to_dict(self):
        return {
            "code": self.code,
            "event": self.event,
            "is_last_package": self.is_last_package,
            "payload_sequence": self.payload_sequence,
            "payload_size": self.payload_size,
            "payload_msg": self.payload_msg,
        }

class ResponseParser:
    @staticmethod
    def parse_response(msg_bytes: bytes) -> AsrResponse:
        resp = AsrResponse()
        header_size = msg_bytes[0] & 0x0f
        message_type = msg_bytes[1] >> 4
        message_type_specific_flags = msg_bytes[1] & 0x0f
        serialization_method = msg_bytes[2] >> 4
        message_compression = msg_bytes[2] & 0x0f
        payload = msg_bytes[header_size*4:]

        if message_type_specific_flags & 0x01:
            resp.payload_sequence = struct.unpack('>i', payload[:4])[0]
            payload = payload[4:]
        if message_type_specific_flags & 0x02:
            resp.is_last_package = True
        if message_type_specific_flags & 0x04:
            resp.event = struct.unpack('>i', payload[:4])[0]
            payload = payload[4:]

        if message_type == MessageType.SERVER_FULL_RESPONSE:
            resp.payload_size = struct.unpack('>I', payload[:4])[0]
            payload = payload[4:]
        elif message_type == MessageType.SERVER_ERROR_RESPONSE:
            resp.code = struct.unpack('>i', payload[:4])[0]
            resp.payload_size = struct.unpack('>I', payload[4:8])[0]
            payload = payload[8:]

        if not payload:
            return resp

        if message_compression == CompressionType.GZIP:
            try:
                payload = gzip.decompress(payload)
            except Exception:
                return resp

        try:
            if serialization_method == SerializationType.JSON:
                resp.payload_msg = json.loads(payload.decode('utf-8'))
        except Exception:
            pass
        return resp

# 会话对象（桥接前端WS与 SAUC 后端WS）
class SaucSession:
    def __init__(self, session_id: str, url: str, resource_id: str, loop):
        self.session_id = session_id
        self.url = url
        self.resource_id = resource_id
        self.seq = 1
        self.loop = loop
        self.http = aiohttp.ClientSession()
        self.backend_ws = None
        self.logid = None
        self.dialog_bridge = None  # 将在 handler 中注入
        # 会话日志目录
        base_dir = os.path.join(os.getcwd(), 'sessions', datetime.now().strftime('%Y%m%d'), session_id)
        os.makedirs(base_dir, exist_ok=True)
        self.base_dir = base_dir
        # 独立文件日志
        self.session_logger = logging.getLogger(f"session.{session_id}")
        self.session_logger.setLevel(logging.INFO)
        fh = logging.FileHandler(os.path.join(base_dir, 'session.log'))
        fh.setFormatter(logging.Formatter('%(asctime)s - %(levelname)s - %(message)s'))
        self.session_logger.addHandler(fh)
        self.responses_path = os.path.join(base_dir, 'responses.jsonl')
        self.frontend_result_log_path = os.path.join(base_dir, 'frontend_result.jsonl')
        # 去重/节流配置与状态
        try:
            self.result_dedup = (os.getenv("RESULT_DEDUP", "true").lower() in ("1", "true", "yes", "y"))
        except Exception:
            self.result_dedup = True
        try:
            self.result_min_delta = int(os.getenv("RESULT_MIN_DELTA", "0"))
        except Exception:
            self.result_min_delta = 0
        try:
            self.result_debounce_ms = int(os.getenv("RESULT_DEBOUNCE_MS", "0"))
        except Exception:
            self.result_debounce_ms = 0
        self._last_text = ""
        self._last_emit_ts = 0
        # 保存元数据
        meta = {
            "session_id": session_id,
            "resource_id": resource_id,
            "backend_url": url,
            "start_time": datetime.now().isoformat(),
        }
        with open(os.path.join(base_dir, 'metadata.json'), 'w', encoding='utf-8') as f:
            json.dump(meta, f, ensure_ascii=False, indent=2)

    async def connect_backend(self):
        headers = {
            "X-Api-Resource-Id": self.resource_id,
            "X-Api-Connect-Id": str(uuid.uuid4()),
            "X-Api-Access-Key": ACCESS_KEY,
            "X-Api-App-Key": APP_KEY,
        }
        try:
            self.backend_ws = await self.http.ws_connect(self.url, headers=headers)
        except aiohttp.WSServerHandshakeError as e:
            # 握手失败时，若当前为 async 端点，则尝试回退到 nonstream 端点
            self.session_logger.error(f"Handshake failed for {self.url}: status={getattr(e, 'status', '?')} message={getattr(e, 'message', '')}")
            if 'bigmodel_async' in self.url:
                fallback = 'wss://openspeech.bytedance.com/api/v3/sauc/bigmodel_nostream'
                self.session_logger.info(f"Falling back to {fallback}")
                self.url = fallback
                self.backend_ws = await self.http.ws_connect(self.url, headers=headers)
            else:
                raise
        except Exception as e:
            # 其他连接异常也尝试一次回退（仅当当前为 async 端点）
            if 'bigmodel_async' in self.url:
                fallback = 'wss://openspeech.bytedance.com/api/v3/sauc/bigmodel_nostream'
                self.session_logger.warning(f"Connect failed for {self.url} ({e}); trying fallback {fallback}")
                self.url = fallback
                self.backend_ws = await self.http.ws_connect(self.url, headers=headers)
            else:
                raise
        try:
            self.logid = self.backend_ws.headers.get('X-Tt-Logid') or self.backend_ws.headers.get('X-Tt-LogId')
        except Exception:
            self.logid = None
        self.session_logger.info(f"Connected to SAUC: logid={self.logid}, connect_id={headers.get('X-Api-Connect-Id')}")

    def _build_full_request(self) -> bytes:
        """
        构建完整请求（JSON + GZIP），根据后端端点动态设置模式参数。
        - 当使用 async/stream 端点时：开启增量（show_utterances=True），禁用非流式（enable_nonstream=False）。
        - 当使用 nonstream 端点时：关闭增量（show_utterances=False），启用非流式（enable_nonstream=True）。
        """
        header = AsrRequestHeader()\
            .with_message_type_specific_flags(MessageTypeSpecificFlags.POS_SEQUENCE)
        use_nonstream = ('bigmodel_nostream' in (self.url or ''))
        payload = {
            "user": {"uid": self.session_id},
            "audio": {
                "format": "pcm",
                "codec": "raw",
                "rate": 16000,
                "bits": 16,
                "channel": 1
            },
            "request": {
                "model_name": "bigmodel",
                "enable_itn": True,
                "enable_punc": True,
                "enable_ddc": True,
                "show_utterances": (not use_nonstream),
                "enable_nonstream": use_nonstream
            }
        }
        payload_bytes = json.dumps(payload).encode('utf-8')
        compressed = gzip.compress(payload_bytes)
        buf = bytearray()
        buf.extend(header.to_bytes())
        buf.extend(struct.pack('>i', self.seq))
        buf.extend(struct.pack('>I', len(compressed)))
        buf.extend(compressed)
        return bytes(buf)

    def _build_audio_request(self, segment: bytes, is_last: bool=False) -> bytes:
        header = AsrRequestHeader()
        header.with_serialization_type(SerializationType.NO_SERIALIZATION)
        if is_last:
            header.with_message_type_specific_flags(MessageTypeSpecificFlags.NEG_WITH_SEQUENCE)
            seq = -self.seq
        else:
            header.with_message_type_specific_flags(MessageTypeSpecificFlags.POS_SEQUENCE)
            seq = self.seq
        header.with_message_type(MessageType.CLIENT_AUDIO_ONLY_REQUEST)
        compressed = gzip.compress(segment)
        buf = bytearray()
        buf.extend(header.to_bytes())
        buf.extend(struct.pack('>i', seq))
        buf.extend(struct.pack('>I', len(compressed)))
        buf.extend(compressed)
        return bytes(buf)

    async def start(self):
        await self.connect_backend()
        # 发送完整请求
        req = self._build_full_request()
        await self.backend_ws.send_bytes(req)
        self.session_logger.info(f"Sent full client request seq={self.seq}")
        # 等待一次响应（握手回应）
        msg = await self.backend_ws.receive()
        if msg.type == aiohttp.WSMsgType.BINARY:
            resp = ResponseParser.parse_response(msg.data)
            self._log_response(resp)
        self.seq += 1

    async def send_audio(self, segment: bytes, is_last: bool=False):
        req = self._build_audio_request(segment, is_last=is_last)
        await self.backend_ws.send_bytes(req)
        self.session_logger.info(f"Sent audio seq={self.seq} last={is_last}")
        if not is_last:
            self.seq += 1

    async def recv_loop(self, frontend_ws: web.WebSocketResponse):
        """
        后端接收循环：从 SAUC 后端 WebSocket 持续读取响应并转发给前端。
        输入：frontend_ws 前端的 WebSocketResponse
        输出：无（通过 frontend_ws 发送文本消息），函数在收到最终包或错误时退出。
        逻辑：
        - 解析后端二进制响应，写入本地日志并转发给前端
        - 在前端连接关闭或写入异常时，停止发送并退出循环
        - 收到最终包（is_last_package=True）或非零 code 时退出
        """
        try:
            async for msg in self.backend_ws:
                if msg.type == aiohttp.WSMsgType.BINARY:
                    resp = ResponseParser.parse_response(msg.data)
                    self._log_response(resp)
                    final = bool(resp.is_last_package)
                    text = extract_final_text(resp.payload_msg)
                    out = {
                        "type": "result",
                        "session_id": self.session_id,
                        "sequence": resp.payload_sequence,
                        "final": final,
                        "text": text,
                        "logid": self.logid,
                    }
                    # 无变化抑制与最小变化阈值/节流
                    if not final and self.result_dedup:
                        now_ms = int(datetime.now().timestamp() * 1000)
                        same = (text == self._last_text)
                        small_change = (not same and self.result_min_delta > 0 and abs(len(text) - len(self._last_text)) < self.result_min_delta)
                        within_debounce = (self.result_debounce_ms > 0 and (now_ms - self._last_emit_ts) < self.result_debounce_ms and same)
                        if same or small_change or within_debounce:
                            # 跳过发送与记录，但仍让对话桥收到最新文本以便断句累积
                            if self.dialog_bridge is not None:
                                try:
                                    await self.dialog_bridge.on_recognition_update(text or "", final)
                                except Exception as e:
                                    self.session_logger.error(f"DialogBridge handling failed (dedup path): {e}")
                            continue
                    # 前端已关闭则停止发送
                    if frontend_ws.closed:
                        self.session_logger.warning("Frontend WS closed; stop forwarding")
                        break
                    try:
                        await frontend_ws.send_str(json.dumps(out, ensure_ascii=False))
                    except (aiohttp.client_exceptions.ClientConnectionResetError, ConnectionResetError):
                        # 前端写端关闭或网络重置，退出循环
                        self.session_logger.warning("Cannot write to closing transport; stop forwarding")
                        break
                    except Exception as e:
                        # 其他写入异常，记录后退出
                        self.session_logger.error(f"Forwarding to frontend failed: {e}")
                        break

                    # 记录返回给前端的 ASR result 消息
                    self._append_frontend_result({
                        "t": datetime.now().isoformat(),
                        "data": out,
                    })
                    # 更新去重状态
                    self._last_text = text
                    self._last_emit_ts = int(datetime.now().timestamp() * 1000)

                    # 将识别更新交由对话桥处理（提交断句到后端对话服务）
                    if self.dialog_bridge is not None:
                        try:
                            await self.dialog_bridge.on_recognition_update(text or "", final)
                        except Exception as e:
                            self.session_logger.error(f"DialogBridge handling failed: {e}")
                    if final or resp.code != 0:
                        break
                elif msg.type == aiohttp.WSMsgType.ERROR:
                    # 尽量通知前端错误，但若前端已关闭需忽略
                    if not frontend_ws.closed:
                        try:
                            await frontend_ws.send_str(json.dumps({
                                "type": "error",
                                "session_id": self.session_id,
                                "message": "backend websocket error"
                            }))
                        except Exception:
                            pass
                    break
                elif msg.type == aiohttp.WSMsgType.CLOSED:
                    break
        except asyncio.CancelledError:
            # 任务被上层取消，正常退出
            pass

    def _log_response(self, resp: AsrResponse):
        with open(self.responses_path, 'a', encoding='utf-8') as f:
            f.write(json.dumps(resp.to_dict(), ensure_ascii=False) + "\n")
        self.session_logger.info(f"Backend resp: {resp.to_dict()}")

    def _append_frontend_result(self, item: dict):
        """记录返回给前端的 type=result 消息（JSON 行）。
        输入：item 字典，包含时间戳和完整消息内容
        输出：写入会话目录下 frontend_result.jsonl 文件
        """
        try:
            with open(self.frontend_result_log_path, 'a', encoding='utf-8') as f:
                f.write(json.dumps(item, ensure_ascii=False) + "\n")
        except Exception:
            pass

    async def close(self):
        try:
            if self.backend_ws and not self.backend_ws.closed:
                await self.backend_ws.close()
        finally:
            await self.http.close()

# WebSocket 处理器
async def ws_asr_handler(request: web.Request):
    # 为后续赋值声明全局变量，避免 Python 作用域冲突
    global APP_KEY, ACCESS_KEY
    # 参数：资源ID与后端URL可通过查询参数指定，否则使用默认（来自 .env）
    url = request.query.get('url', DEFAULT_BACKEND_URL)
    resource_id = request.query.get('resource_id', DEFAULT_RESOURCE_ID)
    # 支持通过查询参数覆盖凭据（用于Linux服务进程没有环境变量时）
    app_key = request.query.get('app_key', APP_KEY)
    access_key = request.query.get('access_key', ACCESS_KEY)
    dialog_url = request.query.get('dialog_url', DEFAULT_DIALOG_URL)
    dialog_mode = request.query.get('dialog_mode', DEFAULT_DIALOG_MODE)
    voice_id = request.query.get('voice_id', DEFAULT_VOICE_ID)
    system_prompt = request.query.get('system_prompt', DEFAULT_SYSTEM_PROMPT)

    # 语音克隆 TTS 接入（允许查询参数覆盖）
    voice_clone_enabled = (request.query.get('voice_clone_enabled', str(DEFAULT_VOICE_CLONE_ENABLED)).lower() in ("1", "true", "yes", "y"))
    voice_clone_url = request.query.get('voice_clone_url', DEFAULT_VOICE_CLONE_URL)
    voice_clone_mode = request.query.get('voice_clone_mode', DEFAULT_VOICE_CLONE_MODE)
    voice_clone_voice_type = request.query.get('voice_clone_voice_type', DEFAULT_VOICE_CLONE_VOICE_TYPE)
    voice_clone_send_final_only = (request.query.get('voice_clone_send_final_only', str(DEFAULT_VOICE_CLONE_SEND_FINAL_ONLY)).lower() in ("1", "true", "yes", "y"))
    try:
        voice_clone_sample_rate = int(request.query.get('voice_clone_sample_rate', str(DEFAULT_VOICE_CLONE_SAMPLE_RATE)))
    except Exception:
        voice_clone_sample_rate = DEFAULT_VOICE_CLONE_SAMPLE_RATE
    voice_clone_save_path = request.query.get('voice_clone_save_path', DEFAULT_VOICE_CLONE_SAVE_PATH)
    voice_clone_save_mode = request.query.get('voice_clone_save_mode', DEFAULT_VOICE_CLONE_SAVE_MODE)

    ws = web.WebSocketResponse()
    await ws.prepare(request)

    session_id = str(uuid.uuid4())
    loop = asyncio.get_event_loop()
    # 运行时凭据校验与友好错误返回
    if not app_key or not access_key or not resource_id:
        # 注意：避免对同一个 request 重复调用 prepare。
        # 这里复用已 prepare 的 ws，直接发送错误并关闭；或者返回 HTTP 错误响应。
        err = {
            "type": "error",
            "session_id": session_id,
            "message": "missing credentials: app_key/access_key/resource_id",
        }
        try:
            # 如果已完成握手，则通过 WS 返回错误并关闭
            await ws.send_str(json.dumps(err))
            await ws.close()
            return ws
        except Exception:
            # 若握手未完成或失败，则返回 JSON 错误响应
            return web.json_response(err, status=400)

    # 将凭据注入全局（简化后续 connect_backend 使用）
    APP_KEY = app_key
    ACCESS_KEY = access_key

    session = SaucSession(session_id, url, resource_id, loop)

    await session.start()
    # 初始化对话桥（桥接识别文本到对话后端）
    session.dialog_bridge = DialogBridge(
        session_id=session_id,
        dialog_url=dialog_url,
        http=session.http,
        frontend_ws=ws,
        logger=session.session_logger,
        system_prompt=system_prompt,
        session_dir=session.base_dir,
        dialog_mode=dialog_mode,
        voice_id=voice_id,
        # 语音克隆 TTS 配置注入
        voice_clone_enabled=voice_clone_enabled,
        voice_clone_url=voice_clone_url,
        voice_clone_mode=voice_clone_mode,
        voice_clone_voice_type=voice_clone_voice_type,
        voice_clone_send_final_only=voice_clone_send_final_only,
        voice_clone_sample_rate=voice_clone_sample_rate,
        voice_clone_save_path=voice_clone_save_path,
        voice_clone_save_mode=voice_clone_save_mode,
    )
    recv_task = asyncio.create_task(session.recv_loop(ws))

    try:
        async for msg in ws:
            if msg.type == aiohttp.WSMsgType.TEXT:
                try:
                    data = json.loads(msg.data)
                except Exception:
                    continue
                if data.get('event') == 'end':
                    # 发送最后一包（空数据也可，表示结束）
                    await session.send_audio(b'', is_last=True)
                elif data.get('event') == 'ping':
                    await ws.send_str(json.dumps({"type": "pong", "session_id": session_id}))
            elif msg.type == aiohttp.WSMsgType.BINARY:
                # 前端需发送 PCM16/16k/mono 原始字节（推荐每包 ~200ms）
                await session.send_audio(msg.data, is_last=False)
            elif msg.type == aiohttp.WSMsgType.ERROR:
                break
            elif msg.type == aiohttp.WSMsgType.CLOSED:
                break
    finally:
        # 优先终止后端接收循环，避免在前端关闭后继续写入
        if recv_task and not recv_task.done():
            recv_task.cancel()
            try:
                await recv_task
            except asyncio.CancelledError:
                pass
        # 关闭对话桥
        if session.dialog_bridge is not None:
            try:
                await session.dialog_bridge.close()
            except Exception:
                pass
        await session.close()
        if not ws.closed:
            await ws.close()

    return ws

# 启动应用
def build_app():
    app = web.Application()
    app.add_routes([web.get('/ws-asr', ws_asr_handler)])

    async def test_client_handler(request: web.Request):
        """
        测试页面路由：提供浏览器端WebSocket录音测试页。
        输入：HTTP GET请求
        输出：返回本地test_client.html静态文件
        """
        from pathlib import Path
        fp = Path(__file__).parent / "test_client.html"
        if not fp.exists():
            return web.Response(status=404, text="test_client.html not found")
        return web.FileResponse(path=str(fp))

    # 提供测试页面访问
    app.add_routes([web.get('/', test_client_handler), web.get('/test_client.html', test_client_handler)])
    return app

# 对话桥：将实时识别文本以自然断句提交给后端对话服务
class DialogBridge:
    def __init__(self, session_id: str, dialog_url: str, http: aiohttp.ClientSession,
                 frontend_ws: web.WebSocketResponse, logger: logging.Logger,
                 system_prompt: str, session_dir: str, dialog_mode: str = "sse", voice_id: str = "demo-voice",
                 strong_punct: str = None, soft_punct: str = None, soft_flush_min_chars: int = None,
                 autoflush_min_chars: int = None, enable_soft_submit: bool = None,
                 # 语音克隆 TTS 配置
                 voice_clone_enabled: bool = False,
                 voice_clone_url: str = "",
                 voice_clone_mode: str = "stream",
                 voice_clone_voice_type: str = "demo",
                 voice_clone_send_final_only: bool = False,
                 voice_clone_sample_rate: int = 24000,
                 voice_clone_save_path: str = "",
                 voice_clone_save_mode: str = "append"):
        """
        对话桥接
        输入：
        - session_id: 会话ID
        - dialog_url: 对话后端URL（POST 接口）
        - http: 共享的 aiohttp.ClientSession
        - frontend_ws: 前端 WebSocket，用于转发对话响应
        - logger: 日志记录器
        - system_prompt: 系统提示词（用于对话后端）
        - session_dir: 会话目录，用于落盘日志
        输出：无
        说明：
        - 维护增量识别的缓冲，根据断句策略提交给对话后端
        - 接收后端响应并转发到前端（type=dialog）
        - 支持错误重试与简单使用量统计记录
        """
        self.session_id = session_id
        self.dialog_url = dialog_url
        self.http = http
        self.frontend_ws = frontend_ws
        self.logger = logger
        self.system_prompt = system_prompt
        self.session_dir = session_dir
        self.dialog_mode = (dialog_mode or "sse").lower()
        self.voice_id = voice_id or "demo-voice"
        self.buffer = ""  # 当前累计文本
        self.last_sent_index = 0  # 最近提交到对话的文本边界索引
        # 可配置断句：强/软标点集合与长度阈值（均可通过环境变量覆盖）
        default_strong = os.getenv("DIALOG_PUNCT_STRONG", "。！？!?….")
        default_soft = os.getenv("DIALOG_PUNCT_SOFT", "，、；;,")
        self.strong_punct = (strong_punct or default_strong)
        self.soft_punct = (soft_punct or default_soft)
        self.re_strong = re.compile("[" + re.escape(self.strong_punct) + "]+")
        self.re_soft = re.compile("[" + re.escape(self.soft_punct) + "]+")
        self.retry_max = 2
        self.usage_tokens = 0
        self.dialog_log_path = os.path.join(session_dir, 'dialog_events.jsonl')
        # 新增：分别记录对话请求与返回给前端的对话消息
        self.dialog_req_log_path = os.path.join(session_dir, 'dialog_requests.jsonl')
        self.frontend_dialog_log_path = os.path.join(session_dir, 'frontend_dialog.jsonl')
        # 自动提交的最小长度阈值（当无强标点、非最终包时避免长时间不提交）
        try:
            env_auto = int(os.getenv("DIALOG_AUTOF_FLUSH_LEN", "12"))
        except Exception:
            env_auto = 12
        try:
            env_soft = int(os.getenv("DIALOG_SOFT_FLUSH_LEN", "20"))
        except Exception:
            env_soft = 20
        self.autoflush_min_chars = env_auto if autoflush_min_chars is None else int(autoflush_min_chars)
        self.soft_flush_min_chars = env_soft if soft_flush_min_chars is None else int(soft_flush_min_chars)
        if enable_soft_submit is None:
            v = os.getenv("DIALOG_ENABLE_SOFT_SUBMIT", "true").lower()
            self.enable_soft_submit = v in ("1", "true", "yes", "y")
        else:
            self.enable_soft_submit = bool(enable_soft_submit)

        # 语音克隆 TTS 配置与前端音频日志
        # 说明：当启用语音克隆时，我们会在每个 reply 段（stream=true）与最终回复（stream=false）
        # 异步调用 TTS 服务，并将返回的 MP3 按到达顺序以 Base64 分片推送到前端。
        self.voice_clone_enabled = bool(voice_clone_enabled)
        self.voice_clone_url = voice_clone_url or ""
        self.voice_clone_mode = (voice_clone_mode or "stream").lower()
        self.voice_clone_voice_type = voice_clone_voice_type or "demo"
        self.voice_clone_send_final_only = bool(voice_clone_send_final_only)
        self.voice_clone_sample_rate = int(voice_clone_sample_rate or 24000)
        self.voice_clone_save_path = voice_clone_save_path or ""
        self.voice_clone_save_mode = (voice_clone_save_mode or "append").lower()
        # 前端音频日志路径与片段序号（单调递增）
        self.frontend_audio_log_path = os.path.join(session_dir, 'frontend_audio.jsonl')
        self.audio_seq = 1

    def _extract_sse_text(self, data_str: str) -> str:
        """
        解析 SSE 的 data 内容，提取可显示的纯文本。
        输入：data_str 原始字符串（可能是 JSON、多个 JSON 拼接，或纯文本）
        输出：提取出的文本（若无法提取则返回空字符串）
        处理策略：
        - 优先按 JSON 解析，常见键：text、delta、content、reply
        - 若解析失败，尝试兼容多个 JSON 拼接（通过正则提取所有 "text" 字段）
        - 忽略仅含 requestId/sessionId 等非文本字段的片段
        - 兼容纯文本直接返回
        """
        if not data_str:
            return ""
        s = data_str.strip()
        # 常见结束标记
        if s.upper() in {"[DONE]", "DONE"}:
            return ""
        # 先尝试标准 JSON
        try:
            obj = json.loads(s)
            if isinstance(obj, dict):
                for key in ("text", "delta", "content", "reply"):
                    val = obj.get(key)
                    if isinstance(val, str):
                        return val
                # OpenAI 风格：choices[0].delta.content
                choices = obj.get("choices")
                if isinstance(choices, list) and choices:
                    delta = choices[0].get("delta") if isinstance(choices[0], dict) else None
                    if isinstance(delta, dict):
                        v = delta.get("content") or delta.get("text")
                        if isinstance(v, str):
                            return v
                return ""
        except Exception:
            pass

        # 兼容多个 JSON 在同一个 data 内拼接，比如 `}{` 连续的情况
        try:
            # 通过正则提取所有 "text": "..." 片段
            texts = re.findall(r'"text"\s*:\s*"([^"\\]*(?:\\.[^"\\]*)*)"', s)
            if texts:
                # 还原转义字符
                cleaned = "".join([bytes(t, 'utf-8').decode('unicode_escape') for t in texts])
                return cleaned
            # 其次提取 content/delta 作为备用
            contents = re.findall(r'"content"\s*:\s*"([^"\\]*(?:\\.[^"\\]*)*)"', s)
            if contents:
                cleaned = "".join([bytes(t, 'utf-8').decode('unicode_escape') for t in contents])
                return cleaned
        except Exception:
            pass

        # 作为最后兜底，若看起来是纯文本（不含明显 JSON 结构），直接返回
        if not ("{" in s and "}" in s):
            return s
        return ""

    async def on_recognition_update(self, text: str, is_final: bool):
        """
        处理识别增量更新
        输入：最新识别的完整文本（可能包含历史）、是否最终包
        输出：必要时将完整句子提交到后端
        策略：
        - 基于最长公共前缀找出新增部分
        - 当新增部分包含强标点（。！？.!?）形成完整句子时提交
        - 若收到最终包，提交剩余未发送文本
        """
        if not text:
            if is_final:
                await self._flush_remaining()
            return

        # 扩展缓冲为最新文本（覆盖式，以服务端最新为准）
        self.buffer = text
        # 找到未发送区间
        unsent = self.buffer[self.last_sent_index:]
        if not unsent and not is_final:
            return

        sentences = self._split_complete_sentences(unsent)
        # 逐句提交（强/软标点拆分）
        for s in sentences:
            await self._submit_to_dialog(s)
            self.last_sent_index += len(s)

        # 若无标点拆分但文本已达到长度阈值，自动提交一次以便对话及时响应
        if not sentences:
            # 尝试使用软标点尾部分割，在超过软阈值时提前提交
            if self.enable_soft_submit and len(unsent) >= self.soft_flush_min_chars:
                soft_seg = self._split_soft_tail(unsent)
                if soft_seg:
                    await self._submit_to_dialog(soft_seg)
                    self.last_sent_index += len(soft_seg)
            # 兜底：长度达到自动提交阈值或最终包
            tail_candidate = unsent.strip()
            if tail_candidate and (is_final or len(tail_candidate) >= self.autoflush_min_chars):
                await self._submit_to_dialog(tail_candidate)
                self.last_sent_index = len(self.buffer)

        # 最终包时提交尾部非完整句
        if is_final:
            tail = self.buffer[self.last_sent_index:].strip()
            if tail:
                await self._submit_to_dialog(tail)
                self.last_sent_index = len(self.buffer)

    def _split_complete_sentences(self, text: str):
        """
        将文本按强标点拆分为完整句子（保留标点）。
        输入：text 文本
        输出：句子列表（每个末尾包含强标点）
        """
        if not text:
            return []
        out = []
        start = 0
        for m in self.re_strong.finditer(text):
            end = m.end()
            sent = text[start:end]
            if sent.strip():
                out.append(sent)
            start = end
        return out

    def _split_soft_tail(self, text: str) -> str:
        """
        在文本尾部查找最后一个软标点分界，返回可提交的片段（保留标点）。
        条件：启用软提交且长度达到 soft_flush_min_chars。
        输入：text 文本
        输出：可提交的尾部片段字符串；若无合适分界返回空串
        """
        if not text or not self.enable_soft_submit:
            return ""
        if len(text) < self.soft_flush_min_chars:
            return ""
        last = None
        for m in self.re_soft.finditer(text):
            last = m
        if last is None:
            return ""
        end = last.end()
        seg = text[:end]
        return seg.strip()

    async def _submit_to_dialog(self, sentence: str):
        """
        提交一句用户输入到对话后端，并将响应转发给前端。
        输入：句子字符串
        输出：无（将响应通过 frontend_ws 发送）
        支持两种模式：
        - json：原有非流式 JSON 接口
        - sse：流式 SSE 接口（Accept: text/event-stream），逐块解析 data: 行
        """
        event = {"type": "dialog_submit", "t": datetime.now().isoformat(), "payload": {"sentence": sentence, "mode": self.dialog_mode}}
        self._append_dialog_log(event)

        attempt = 0
        while attempt <= self.retry_max:
            try:
                if self.dialog_mode == "sse":
                    # SSE 模式：按请求示例进行 POST
                    headers = {
                        "Accept": "text/event-stream",
                        "Content-Type": "application/json",
                        "X-Voice-Id": self.voice_id,
                    }
                    body = {"input": sentence, "sessionId": self.session_id}
                    # 记录将要发送的请求
                    self._append_dialog_request({
                        "t": datetime.now().isoformat(),
                        "mode": "sse",
                        "url": self.dialog_url,
                        "headers": headers,
                        "body": body,
                    })
                    async with self.http.post(self.dialog_url, headers=headers, json=body, timeout=30) as resp:
                        if (resp.status // 100) != 2:
                            text = await resp.text()
                            raise RuntimeError(f"Dialog SSE HTTP {resp.status}: {text}")
                        # 逐块读取 SSE：维护行缓冲，提取纯文本，断句后推送前端
                        full_reply = []
                        buffer = ""
                        line_buf = ""  # SSE 行缓冲，避免半行导致解析异常
                        async for chunk, _ in resp.content.iter_chunks():
                            try:
                                decoded = chunk.decode('utf-8', errors='ignore')
                            except Exception:
                                continue
                            if not decoded:
                                continue
                            line_buf += decoded
                            # 按行处理，保留最后一段残缺到下一轮
                            while True:
                                if "\n" not in line_buf:
                                    break
                                ln, line_buf = line_buf.split("\n", 1)
                                sline = ln.strip()
                                if not sline:
                                    continue
                                if sline.startswith('data:'):
                                    payload = sline[5:].strip()
                                    text_piece = self._extract_sse_text(payload)
                                    if not text_piece:
                                        continue
                                    buffer += text_piece
                                    full_reply.append(text_piece)
                                    # 根据断句规则拆分完整句（强标点），无则在软阈值下尝试软分割
                                    sentences = self._split_complete_sentences(buffer)
                                    consumed = 0
                                    if not sentences and self.enable_soft_submit and len(buffer) >= self.soft_flush_min_chars:
                                        soft_seg = self._split_soft_tail(buffer)
                                        sentences = [soft_seg] if soft_seg else []
                                    if sentences:
                                        for seg in sentences:
                                            consumed += len(seg)
                                            out = {
                                                "type": "dialog",
                                                "session_id": self.session_id,
                                                "user_sentence": sentence,
                                                "reply": seg,
                                                "usage": {},
                                                "stream": True,
                                            }
                                            if not self.frontend_ws.closed:
                                                try:
                                                    await self.frontend_ws.send_str(json.dumps(out, ensure_ascii=False))
                                                except Exception:
                                                    pass
                                            # 记录返回给前端的对话增量消息
                                            self._append_frontend_dialog({
                                                "t": datetime.now().isoformat(),
                                                "data": out,
                                            })
                                            # 触发语音克隆（增量段），按配置控制
                                            if self.voice_clone_enabled and not self.voice_clone_send_final_only and self.voice_clone_url:
                                                try:
                                                    asyncio.create_task(self._emit_tts(seg))
                                                except Exception:
                                                    pass
                                        # 截断已发送的部分，保留尾部未成句文本
                                        buffer = buffer[consumed:]
                        # 汇总发送最终结果（保留兼容：发送完整回复）
                        final = "".join(full_reply).strip()
                        out = {
                            "type": "dialog",
                            "session_id": self.session_id,
                            "user_sentence": sentence,
                            "reply": final,
                            "usage": {},
                            "stream": False,
                        }
                        if not self.frontend_ws.closed:
                            try:
                                await self.frontend_ws.send_str(json.dumps(out, ensure_ascii=False))
                            except Exception:
                                pass
                        # 记录返回给前端的最终对话消息
                        self._append_frontend_dialog({
                            "t": datetime.now().isoformat(),
                            "data": out,
                        })
                        # 最终回复触发语音克隆
                        if self.voice_clone_enabled and self.voice_clone_url:
                            try:
                                asyncio.create_task(self._emit_tts(final))
                            except Exception:
                                pass
                        self._append_dialog_log({"type": "dialog_response", "t": datetime.now().isoformat(), "data": {"reply": final, "mode": "sse"}})
                        return
                else:
                    # JSON 模式（保留兼容）
                    payload = {
                        "session_id": self.session_id,
                        "role": "user",
                        "content": sentence,
                        "system_prompt": self.system_prompt,
                    }
                    # 记录将要发送的请求
                    self._append_dialog_request({
                        "t": datetime.now().isoformat(),
                        "mode": "json",
                        "url": self.dialog_url,
                        "headers": {"Content-Type": "application/json"},
                        "body": payload,
                    })
                    async with self.http.post(self.dialog_url, json=payload, timeout=20) as resp:
                        ok = (resp.status // 100) == 2
                        data = None
                        try:
                            data = await resp.json()
                        except Exception:
                            raw = await resp.text()
                            data = {"raw": raw}
                        if not ok:
                            raise RuntimeError(f"Dialog HTTP {resp.status}: {data}")
                        reply = data.get("reply") or data.get("content") or data.get("text") or ""
                        usage = data.get("usage") or {}
                        self._record_usage(usage)
                        if not self.frontend_ws.closed:
                            out = {
                                "type": "dialog",
                                "session_id": self.session_id,
                                "user_sentence": sentence,
                                "reply": reply,
                                "usage": usage,
                            }
                            try:
                                await self.frontend_ws.send_str(json.dumps(out, ensure_ascii=False))
                            except Exception:
                                pass
                        # 记录返回给前端的对话消息
                        self._append_frontend_dialog({
                            "t": datetime.now().isoformat(),
                            "data": out,
                        })
                        # JSON 模式下也触发语音克隆（按配置）
                        if self.voice_clone_enabled and self.voice_clone_url:
                            try:
                                asyncio.create_task(self._emit_tts(reply))
                            except Exception:
                                pass
                        self._append_dialog_log({"type": "dialog_response", "t": datetime.now().isoformat(), "data": data})
                        return
            except Exception as e:
                self.logger.warning(f"Dialog submit failed (attempt {attempt+1}): {e}")
                self._append_dialog_log({"type": "dialog_error", "t": datetime.now().isoformat(), "error": str(e)})
                attempt += 1
                await asyncio.sleep(min(2 ** attempt, 5))

    async def _flush_remaining(self):
        tail = self.buffer[self.last_sent_index:].strip()
        if tail:
            await self._submit_to_dialog(tail)
            self.last_sent_index = len(self.buffer)

    def _record_usage(self, usage: dict):
        """记录使用量（若后端返回 token 计数则累计）"""
        try:
            total = int(usage.get("total_tokens") or 0)
            if total:
                self.usage_tokens += total
        except Exception:
            pass

    def _append_dialog_log(self, item: dict):
        try:
            with open(self.dialog_log_path, 'a', encoding='utf-8') as f:
                f.write(json.dumps(item, ensure_ascii=False) + "\n")
        except Exception:
            pass

    def _append_dialog_request(self, item: dict):
        """记录发送到 /srv/chat 服务的请求（JSON 行）。"""
        try:
            with open(self.dialog_req_log_path, 'a', encoding='utf-8') as f:
                f.write(json.dumps(item, ensure_ascii=False) + "\n")
        except Exception:
            pass

    def _append_frontend_audio(self, item: dict):
        """
        记录返回给前端的音频消息（JSON 行）。
        输入：消息字典（包含 type=audio / session_id / seq / stream / data_b64 等）
        输出：无（落盘到 frontend_audio.jsonl）
        """
        try:
            with open(self.frontend_audio_log_path, 'a', encoding='utf-8') as f:
                f.write(json.dumps(item, ensure_ascii=False) + "\n")
        except Exception:
            pass

    async def _emit_tts(self, text: str):
        """
        语音克隆入口：根据配置的 voice_clone_mode 调用不同的 TTS 方式。
        输入：text 文本
        输出：无（将返回的音频分片或错误信息推送到前端，并记录到 frontend_audio.jsonl）
        逻辑：
        - 当 voice_clone_mode == 'segments_http' 时，调用基于 NDJSON 的 /api/tts/stream_segments
        - 其他情况（默认）使用 JSON 的单次文本直流式 /api/tts/stream
        """
        if not (self.voice_clone_enabled and self.voice_clone_url and text):
            return
        mode = (self.voice_clone_mode or 'stream').lower()
        if mode == 'segments_http':
            await self._emit_tts_http_stream_segments(text)
        else:
            await self._emit_tts_http_stream(text)

    async def _emit_tts_http_stream(self, text: str):
        """
        调用语音克隆 TTS（HTTP 单次文本直流式），将返回的 MP3 音频分片推送到前端。
        输入：text 文本
        输出：无（逐片以 Base64 发送到前端并记录日志）
        行为：
        - 仅在 voice_clone_enabled 为 True 且 voice_clone_url 非空时执行
        - 请求体使用下划线字段：{"text": ..., "session_id": ..., "voice_type": ..., "save_path": ...}
        - 当返回 Content-Type=audio/mpeg 时，按到达顺序逐块读取并发送
        - 当返回 application/json 时，视为错误，解析后记录并向前端发送错误消息
        """
        if not (self.voice_clone_enabled and self.voice_clone_url and text):
            return
        # 记录一次调用尝试（便于会话级排查）
        try:
            self.logger.info(f"TTS start: url={self.voice_clone_url}, text_len={len(text)}, voice_type={self.voice_clone_voice_type}")
        except Exception:
            pass
        body = {"text": text, "session_id": self.session_id}
        if self.voice_clone_voice_type:
            body["voice_type"] = self.voice_clone_voice_type
        if self.voice_clone_save_path:
            body["save_path"] = self.voice_clone_save_path
        headers = {"Content-Type": "application/json"}
        try:
            async with self.http.post(self.voice_clone_url, json=body, headers=headers, timeout=60) as resp:
                ctype = (resp.headers.get('Content-Type') or '').lower()
                ok = (resp.status // 100) == 2
                if not ok:
                    err_msg = None
                    try:
                        err_json = await resp.json()
                        err_msg = json.dumps(err_json, ensure_ascii=False)
                    except Exception:
                        err_msg = await resp.text()
                    if not self.frontend_ws.closed:
                        try:
                            await self.frontend_ws.send_str(json.dumps({
                                "type": "error",
                                "session_id": self.session_id,
                                "message": f"TTS HTTP {resp.status}: {err_msg}",
                            }, ensure_ascii=False))
                        except Exception:
                            pass
                    # 记录错误（JSON 尝试）
                    self._append_frontend_audio({
                        "t": datetime.now().isoformat(),
                        "error": True,
                        "status": resp.status,
                        "message": err_msg,
                        "attempt": "json",
                    })
                    # 尝试回退为 text/plain 纯文本提交（部分 TTS 服务不接受 JSON）
                    try:
                        await self._emit_tts_http_stream_fallback_text(text)
                    except Exception:
                        pass
                    return
                if 'audio/mpeg' in ctype:
                    async for chunk, _ in resp.content.iter_chunks():
                        if not chunk:
                            continue
                        try:
                            b64 = base64.b64encode(chunk).decode('ascii')
                        except Exception:
                            continue
                        out = {
                            "type": "audio",
                            "session_id": self.session_id,
                            "seq": self.audio_seq,
                            "stream": True,
                            "mime": "audio/mpeg",
                            "sample_rate": self.voice_clone_sample_rate,
                            "data_b64": b64,
                        }
                        self.audio_seq += 1
                        if not self.frontend_ws.closed:
                            try:
                                await self.frontend_ws.send_str(json.dumps(out, ensure_ascii=False))
                            except Exception:
                                pass
                        self._append_frontend_audio({
                            "t": datetime.now().isoformat(),
                            "data": out,
                        })
                    return
                else:
                    err_msg = None
                    try:
                        err_json = await resp.json()
                        err_msg = json.dumps(err_json, ensure_ascii=False)
                    except Exception:
                        err_msg = await resp.text()
                    if not self.frontend_ws.closed:
                        try:
                            await self.frontend_ws.send_str(json.dumps({
                                "type": "error",
                                "session_id": self.session_id,
                                "message": f"TTS content-type not audio/mpeg: {ctype}; detail: {err_msg}",
                            }, ensure_ascii=False))
                        except Exception:
                            pass
                    self._append_frontend_audio({
                        "t": datetime.now().isoformat(),
                        "error": True,
                        "status": resp.status,
                        "message": f"ctype={ctype}; {err_msg}",
                        "attempt": "json",
                    })
                    # 同样回退一次纯文本提交
                    try:
                        await self._emit_tts_http_stream_fallback_text(text)
                    except Exception:
                        pass
        except Exception as e:
            if not self.frontend_ws.closed:
                try:
                    await self.frontend_ws.send_str(json.dumps({
                        "type": "error",
                        "session_id": self.session_id,
                        "message": f"TTS request failed: {e}",
                    }, ensure_ascii=False))
                except Exception:
                    pass
            self._append_frontend_audio({
                "t": datetime.now().isoformat(),
                "error": True,
                "message": str(e),
            })

    async def _emit_tts_http_stream_segments(self, text: str):
        """
        调用语音克隆 TTS（HTTP NDJSON 段式，/api/tts/stream_segments），将返回的 MP3 音频分片推送到前端。
        输入：text 文本
        输出：无（逐片以 Base64 发送到前端并记录日志）
        行为：
        - 请求体使用 NDJSON（一行一条 JSON）：{"text": "..."}\n
        - 通过 query 传递 session_id、voice_type、save_path、save_mode（若配置提供）
        - 当返回 Content-Type=audio/mpeg 时，按到达顺序逐块读取并发送
        - 非 audio/mpeg 时，记录错误并落盘
        """
        if not (self.voice_clone_enabled and self.voice_clone_url and text):
            return
        try:
            self.logger.info(f"TTS segments start: url={self.voice_clone_url}, text_len={len(text)}, voice_type={self.voice_clone_voice_type}")
        except Exception:
            pass
        # NDJSON 一行
        line = json.dumps({"text": text}, ensure_ascii=False) + "\n"
        data = line.encode('utf-8')
        params = {}
        if self.session_id:
            params["session_id"] = self.session_id
        if self.voice_clone_voice_type:
            params["voice_type"] = self.voice_clone_voice_type
        if self.voice_clone_save_path:
            params["save_path"] = self.voice_clone_save_path
        if self.voice_clone_save_mode:
            params["save_mode"] = self.voice_clone_save_mode
        headers = {"Content-Type": "application/x-ndjson"}
        try:
            async with self.http.post(self.voice_clone_url, params=params, data=data, headers=headers, timeout=60) as resp:
                ctype = (resp.headers.get('Content-Type') or '').lower()
                ok = (resp.status // 100) == 2
                if not ok:
                    msg = None
                    try:
                        msg = await resp.text()
                    except Exception:
                        msg = f"HTTP {resp.status}"
                    if not self.frontend_ws.closed:
                        try:
                            await self.frontend_ws.send_str(json.dumps({
                                "type": "error",
                                "session_id": self.session_id,
                                "message": f"TTS segments HTTP {resp.status}: {msg}",
                            }, ensure_ascii=False))
                        except Exception:
                            pass
                    self._append_frontend_audio({
                        "t": datetime.now().isoformat(),
                        "error": True,
                        "status": resp.status,
                        "message": msg,
                        "attempt": "segments_http",
                    })
                    return
                if 'audio/mpeg' in ctype:
                    async for chunk, _ in resp.content.iter_chunks():
                        if not chunk:
                            continue
                        try:
                            b64 = base64.b64encode(chunk).decode('ascii')
                        except Exception:
                            continue
                        out = {
                            "type": "audio",
                            "session_id": self.session_id,
                            "seq": self.audio_seq,
                            "stream": True,
                            "mime": "audio/mpeg",
                            "sample_rate": self.voice_clone_sample_rate,
                            "data_b64": b64,
                        }
                        self.audio_seq += 1
                        if not self.frontend_ws.closed:
                            try:
                                await self.frontend_ws.send_str(json.dumps(out, ensure_ascii=False))
                            except Exception:
                                pass
                        self._append_frontend_audio({
                            "t": datetime.now().isoformat(),
                            "data": out,
                        })
                else:
                    msg = None
                    try:
                        msg = await resp.text()
                    except Exception:
                        msg = f"content-type={ctype}"
                    if not self.frontend_ws.closed:
                        try:
                            await self.frontend_ws.send_str(json.dumps({
                                "type": "error",
                                "session_id": self.session_id,
                                "message": f"TTS segments content-type not audio/mpeg: {ctype}; detail: {msg}",
                            }, ensure_ascii=False))
                        except Exception:
                            pass
                    self._append_frontend_audio({
                        "t": datetime.now().isoformat(),
                        "error": True,
                        "status": resp.status,
                        "message": f"ctype={ctype}; {msg}",
                        "attempt": "segments_http",
                    })
        except Exception as e:
            if not self.frontend_ws.closed:
                try:
                    await self.frontend_ws.send_str(json.dumps({
                        "type": "error",
                        "session_id": self.session_id,
                        "message": f"TTS segments request failed: {e}",
                    }, ensure_ascii=False))
                except Exception:
                    pass
            self._append_frontend_audio({
                "t": datetime.now().isoformat(),
                "error": True,
                "message": str(e),
                "attempt": "segments_http",
            })

    async def _emit_tts_http_stream_fallback_text(self, text: str):
        """
        回退方案：以 text/plain 提交纯文本，兼容不支持 JSON 的 TTS 服务。
        输入：text 文本
        输出：若返回 audio/mpeg，则按流式分片推送；否则记录错误。
        """
        try:
            if not (self.voice_clone_enabled and self.voice_clone_url and text):
                return
            try:
                self.logger.info(f"TTS fallback(text/plain) start: url={self.voice_clone_url}, text_len={len(text)}")
            except Exception:
                pass
            headers = {"Content-Type": "text/plain"}
            async with self.http.post(self.voice_clone_url, data=text.encode('utf-8'), headers=headers, timeout=60) as resp:
                ctype = (resp.headers.get('Content-Type') or '').lower()
                ok = (resp.status // 100) == 2
                if not ok:
                    msg = await resp.text()
                    self._append_frontend_audio({
                        "t": datetime.now().isoformat(),
                        "error": True,
                        "status": resp.status,
                        "message": msg,
                        "attempt": "text/plain",
                    })
                    return
                if 'audio/mpeg' in ctype:
                    async for chunk, _ in resp.content.iter_chunks():
                        if not chunk:
                            continue
                        try:
                            b64 = base64.b64encode(chunk).decode('ascii')
                        except Exception:
                            continue
                        out = {
                            "type": "audio",
                            "session_id": self.session_id,
                            "seq": self.audio_seq,
                            "stream": True,
                            "mime": "audio/mpeg",
                            "sample_rate": self.voice_clone_sample_rate,
                            "data_b64": b64,
                        }
                        self.audio_seq += 1
                        if not self.frontend_ws.closed:
                            try:
                                await self.frontend_ws.send_str(json.dumps(out, ensure_ascii=False))
                            except Exception:
                                pass
                        self._append_frontend_audio({
                            "t": datetime.now().isoformat(),
                            "data": out,
                        })
                else:
                    msg = await resp.text()
                    self._append_frontend_audio({
                        "t": datetime.now().isoformat(),
                        "error": True,
                        "status": resp.status,
                        "message": f"ctype={ctype}; {msg}",
                        "attempt": "text/plain",
                    })
        except Exception as e:
            self._append_frontend_audio({
                "t": datetime.now().isoformat(),
                "error": True,
                "message": f"fallback text/plain failed: {e}",
                "attempt": "text/plain",
            })

    def _append_frontend_dialog(self, item: dict):
        """记录返回给前端的 type=dialog 消息（JSON 行）。"""
        try:
            with open(self.frontend_dialog_log_path, 'a', encoding='utf-8') as f:
                f.write(json.dumps(item, ensure_ascii=False) + "\n")
        except Exception:
            pass

    async def close(self):
        # 可在此做资源清理或汇总
        summary = {"type": "dialog_summary", "t": datetime.now().isoformat(), "usage_tokens": self.usage_tokens}
        self._append_dialog_log(summary)

if __name__ == '__main__':
    import argparse
    parser = argparse.ArgumentParser(description='Real-time ASR WebSocket Server (SAUC v3 bridge)')
    # 允许通过环境变量控制服务监听地址与端口（命令行可覆盖）
    parser.add_argument('--host', default=os.getenv('ASR_HOST', '0.0.0.0'))
    parser.add_argument('--port', type=int, default=int(os.getenv('ASR_PORT', '8081')))
    args = parser.parse_args()
    app = build_app()
    web.run_app(app, host=args.host, port=args.port)
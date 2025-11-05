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
    """从项目根目录加载 .env 并合并到环境变量（若存在）。"""
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
                    # 若当前进程环境缺少该变量则注入
                    if key and val and (key not in os.environ or not os.getenv(key)):
                        os.environ[key] = val
        except Exception:
            pass

# 先加载 .env，再读取环境变量或 demo 配置
load_env_file()
try:
    import sauc_websocket_demo as demo
    APP_KEY = demo.config.app_key
    ACCESS_KEY = demo.config.access_key
    DEFAULT_RESOURCE_ID = demo.config.resource_id
except Exception:
    APP_KEY = os.getenv("APP_KEY", "")
    ACCESS_KEY = os.getenv("ACCESS_KEY", "")
    DEFAULT_RESOURCE_ID = os.getenv("RESOURCE_ID", "volc.bigasr.sauc.duration")

# 对话服务配置（可通过环境变量或查询参数覆盖）
DEFAULT_DIALOG_URL = os.getenv("DIALOG_URL", "http://0.0.0.0:8084/chat/stream")
DEFAULT_DIALOG_MODE = os.getenv("DIALOG_MODE", "sse")  # 可选：sse 或 json
DEFAULT_VOICE_ID = os.getenv("VOICE_ID", "demo-voice")
DEFAULT_SYSTEM_PROMPT = os.getenv("SYSTEM_PROMPT", "你是一个礼貌且简洁的助手。")

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
    # 参数：资源ID与后端URL可通过查询参数指定，否则使用默认
    url = request.query.get('url', 'wss://openspeech.bytedance.com/api/v3/sauc/bigmodel_async')
    resource_id = request.query.get('resource_id', DEFAULT_RESOURCE_ID)
    # 支持通过查询参数覆盖凭据（用于Linux服务进程没有环境变量时）
    app_key = request.query.get('app_key', APP_KEY)
    access_key = request.query.get('access_key', ACCESS_KEY)
    dialog_url = request.query.get('dialog_url', DEFAULT_DIALOG_URL)
    dialog_mode = request.query.get('dialog_mode', DEFAULT_DIALOG_MODE)
    voice_id = request.query.get('voice_id', DEFAULT_VOICE_ID)
    system_prompt = request.query.get('system_prompt', DEFAULT_SYSTEM_PROMPT)

    ws = web.WebSocketResponse()
    await ws.prepare(request)

    session_id = str(uuid.uuid4())
    loop = asyncio.get_event_loop()
    # 运行时凭据校验与友好错误返回
    if not app_key or not access_key or not resource_id:
        err = {
            "type": "error",
            "session_id": session_id,
            "message": "missing credentials: app_key/access_key/resource_id",
        }
        ws = web.WebSocketResponse()
        await ws.prepare(request)
        await ws.send_str(json.dumps(err))
        await ws.close()
        return ws

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
                 system_prompt: str, session_dir: str, dialog_mode: str = "sse", voice_id: str = "demo-voice"):
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
        # 断句规则：强标点 + 常用中文顿号/逗号/分号
        # 注意：加入中文逗号可能带来更频繁的提交，但更贴近实时对话期待
        self.re_punct = re.compile(r"[。！？!?…\.，、；;]+")
        self.retry_max = 2
        self.usage_tokens = 0
        self.dialog_log_path = os.path.join(session_dir, 'dialog_events.jsonl')
        # 新增：分别记录对话请求与返回给前端的对话消息
        self.dialog_req_log_path = os.path.join(session_dir, 'dialog_requests.jsonl')
        self.frontend_dialog_log_path = os.path.join(session_dir, 'frontend_dialog.jsonl')
        # 自动提交的最小长度阈值（当无强标点、非最终包时避免长时间不提交）
        try:
            self.autoflush_min_chars = int(os.getenv("DIALOG_AUTOF_FLUSH_LEN", "12"))
        except Exception:
            self.autoflush_min_chars = 12

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
        """将文本按强标点拆分为完整句子（保留标点）"""
        if not text:
            return []
        out = []
        start = 0
        for m in self.re_punct.finditer(text):
            end = m.end()
            sent = text[start:end]
            if sent.strip():
                out.append(sent)
            start = end
        return out

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
                        # 逐块读取 SSE
                        full_reply = []
                        async for chunk, _ in resp.content.iter_chunks():
                            try:
                                line = chunk.decode('utf-8', errors='ignore')
                            except Exception:
                                continue
                            for raw in line.splitlines():
                                s = raw.strip()
                                if not s:
                                    continue
                                # 常见格式：data: <payload>
                                if s.startswith('data:'):
                                    data_str = s[5:].strip()
                                    # 直接转发增量文本（不强制要求 JSON）
                                    out = {
                                        "type": "dialog",
                                        "session_id": self.session_id,
                                        "user_sentence": sentence,
                                        "reply": data_str,
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
                                    full_reply.append(data_str)
                        # 汇总发送最终结果
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
    parser.add_argument('--host', default='0.0.0.0')
    parser.add_argument('--port', type=int, default=8081)
    args = parser.parse_args()
    app = build_app()
    web.run_app(app, host=args.host, port=args.port)
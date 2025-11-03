import asyncio
import aiohttp
from aiohttp import web
import json
import gzip
import struct
import uuid
import logging
import os
from datetime import datetime

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
try:
    import sauc_websocket_demo as demo
    APP_KEY = demo.config.app_key
    ACCESS_KEY = demo.config.access_key
    DEFAULT_RESOURCE_ID = demo.config.resource_id
except Exception:
    APP_KEY = os.getenv("APP_KEY", "")
    ACCESS_KEY = os.getenv("ACCESS_KEY", "")
    DEFAULT_RESOURCE_ID = os.getenv("RESOURCE_ID", "volc.bigasr.sauc.duration")

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
        self.backend_ws = await self.http.ws_connect(self.url, headers=headers)
        try:
            self.logid = self.backend_ws.headers.get('X-Tt-Logid') or self.backend_ws.headers.get('X-Tt-LogId')
        except Exception:
            self.logid = None
        self.session_logger.info(f"Connected to SAUC: logid={self.logid}, connect_id={headers.get('X-Api-Connect-Id')}")

    def _build_full_request(self) -> bytes:
        header = AsrRequestHeader()\
            .with_message_type_specific_flags(MessageTypeSpecificFlags.POS_SEQUENCE)
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
                "model_name": "bigmodel",           # 大模型
                "enable_itn": True,
                "enable_punc": True,
                "enable_ddc": True,
                "show_utterances": True,             # 开启增量结果
                "enable_nonstream": False            # 采用双向流式
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
    # 参数：资源ID与后端URL可通过查询参数指定，否则使用默认
    url = request.query.get('url', 'wss://openspeech.bytedance.com/api/v3/sauc/bigmodel_async')
    resource_id = request.query.get('resource_id', DEFAULT_RESOURCE_ID)

    ws = web.WebSocketResponse()
    await ws.prepare(request)

    session_id = str(uuid.uuid4())
    loop = asyncio.get_event_loop()
    session = SaucSession(session_id, url, resource_id, loop)

    await session.start()
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

if __name__ == '__main__':
    import argparse
    parser = argparse.ArgumentParser(description='Real-time ASR WebSocket Server (SAUC v3 bridge)')
    parser.add_argument('--host', default='0.0.0.0')
    parser.add_argument('--port', type=int, default=8081)
    args = parser.parse_args()
    app = build_app()
    web.run_app(app, host=args.host, port=args.port)
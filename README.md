# Volcengine SAUC v3 大模型流式语音识别 Demo（Windows）

本仓库包含 `sauc_websocket_demo.py` 脚本，用于在 Windows 环境下调用火山引擎开放平台的「大模型流式 ASR」WebSocket API。已为你集成以下配置：
- APP ID（App Key）：5142285262
- Access Token（Access Key）：3hpVlzSZZkLakcOEMsfKDcDDWWdKCxpb
- Secret Key：XvE8oA0RSecmzf5RB45Ln3eTvNQFDT8b（当前协议未用到，保留）

脚本默认使用优化双向流式接口：`bigmodel_async`（更适合实时增量返回）。如果你的账号未开通该端点或握手失败，后端服务会自动回退到 `bigmodel_nostream`（流式输入模式，发送最后一包后返回最终结果）。

---

## 环境准备

- 操作系统：Windows 10/11
- Python：3.8 及以上
- 依赖：
  - `aiohttp`（WebSocket 客户端）
  - 系统需安装 `ffmpeg`（用于自动将非 WAV 音频转为 16kHz/mono/PCM16 WAV）

### 安装依赖

1) 安装 Python 依赖

```
pip install aiohttp
```

2) 安装 ffmpeg（任选一种方式）
- 通过包管理器安装（推荐）：
  - 使用 scoop（需先安装 Scoop）：
    ```powershell
    scoop install ffmpeg
    ```
  - 使用 winget：
    ```powershell
    winget install --id=Gyan.FFmpeg -e
    ```
- 或者到官方站点下载可执行文件并添加到 PATH：
  - https://ffmpeg.org/download.html

安装完成后，执行 `ffmpeg -version` 确认可以正常调用。

---

## 配置说明

脚本内已写入以下信息：
- 认证信息：`X-Api-App-Key`、`X-Api-Access-Key`（来自你的 App ID 与 Access Token）。
- 资源 ID（Resource ID）：默认 `volc.bigasr.sauc.duration`（小时版）。如果你已开通并发版，请在命令行参数中改为 `volc.bigasr.sauc.concurrent`。

请注意：实际生产环境不建议将密钥硬编码在代码中。请将它们放入环境变量或安全配置中心，并在脚本中读取。

---

## 使用方法

1) 准备音频文件：
- 支持常见音频格式（wav/mp3/m4a/flac 等）。脚本会自动调用 ffmpeg 转码为 16kHz/单声道/PCM16 的 WAV。
- 为了获得更高识别准确率，建议原始音频尽量清晰、语速适中。

2) 运行脚本（PowerShell）：

```
python f:\work\singa\spark_asr\sauc_websocket_demo.py --file <你的音频文件路径>
```

- 默认接口 URL：`wss://openspeech.bytedance.com/api/v3/sauc/bigmodel_async`（实时增量）
- 默认分包时长：`--seg-duration 200`（推荐 200ms）
- 可选资源 ID：
  - 小时版：`--resource-id volc.bigasr.sauc.duration`
  - 并发版：`--resource-id volc.bigasr.sauc.concurrent`

示例：

```
python f:\work\singa\spark_asr\sauc_websocket_demo.py --file f:\data\audio\test.wav --seg-duration 200 --resource-id volc.bigasr.sauc.duration
```

运行后会在控制台与 `run.log` 中输出识别结果与连接日志（包含 `X-Tt-Logid` 与 `X-Api-Connect-Id`，便于排查问题）。

---

## 接口模式说明

- 双向流式：`wss://openspeech.bytedance.com/api/v3/sauc/bigmodel`
  - 每输入一个包返回一个包，延迟更低。
- 优化双向流式：`wss://openspeech.bytedance.com/api/v3/sauc/bigmodel_async`
  - 更高并发、更友好的流控策略。
- 流式输入模式（最高准确率）：`wss://openspeech.bytedance.com/api/v3/sauc/bigmodel_nostream`
  - 当累计输入音频 > 15s 或发送最后一包（负序列包）后返回识别结果，准确率更高。

完整请求 payload 会根据端点类型动态设置：
- 对 async/stream 端点：`show_utterances=True`、`enable_nonstream=False`
- 对 nonstream 端点：`show_utterances=False`、`enable_nonstream=True`

---

## 使用示例

- 小时版资源（默认最高准确率、仅打印最终文本）
  - `python sauc_websocket_demo.py --file test.mp3 --resource-id volc.bigasr.sauc.duration --output-mode final`

- 并发版资源（默认最高准确率、仅打印最终文本）
  - `python sauc_websocket_demo.py --file test.mp3 --resource-id volc.bigasr.sauc.concurrent --output-mode final`

- 打印所有服务端响应（调试用）
  - `python sauc_websocket_demo.py --file test.mp3 --resource-id volc.bigasr.sauc.duration --output-mode all`

- 可选：调整分包时长（例如 100ms）
  - `python sauc_websocket_demo.py --file test.mp3 --seg-duration 100`

---

## 实时流式识别服务（WebSocket）

本仓库新增 `sauc_asr_server.py`，提供一个后端 WebSocket 服务，供前端推送音频实现实时流式识别。该服务会将每一次连接作为一个独立会话（session），在 `sessions/<YYYYMMDD>/<session_id>/` 目录下保存日志与响应。

- 启动服务：
  - `python sauc_asr_server.py --host 0.0.0.0 --port 8081`
- 接入地址：
  - `ws://<host>:8081/ws-asr?resource_id=volc.bigasr.sauc.duration`
  - 可选：指定后端端点
    - async（默认）：`&url=wss://openspeech.bytedance.com/api/v3/sauc/bigmodel_async`
    - nonstream：`&url=wss://openspeech.bytedance.com/api/v3/sauc/bigmodel_nostream`
  - 可选：覆盖凭据（仅用于本地联调，不建议生产使用）
    - 通过查询参数：`&app_key=<你的APP_KEY>&access_key=<你的ACCESS_KEY>`
- 会话日志：
  - `session.log`：会话级日志
  - `responses.jsonl`：后端 ASR 响应（JSON 行）
  - `metadata.json`：连接元数据（resource_id、backend_url、session_id、logid）

#### 对话后端（SSE / JSON）

- 默认对话后端：`DIALOG_URL=http://0.0.0.0:8084/chat/stream`
- 模式选择：`DIALOG_MODE=sse`（默认）或 `json`
- 语音风格：`VOICE_ID=demo-voice`（通过请求头 `X-Voice-Id` 传递，SSE 模式生效）
- 前端可通过查询参数覆盖：
  - `ws://<host>:8081/ws-asr?dialog_url=http://0.0.0.0:8084/chat/stream&dialog_mode=sse&voice_id=demo-voice`

断句与流式回复控制（可配置）：
- 环境变量（或在初始化时传参覆盖）：
  - `DIALOG_PUNCT_STRONG`：强标点集合（默认：`。！？!?….`）用于生成完整句
  - `DIALOG_PUNCT_SOFT`：软标点集合（默认：`，、；;,`）用于在达到长度阈值时提前提交
  - `DIALOG_AUTOF_FLUSH_LEN`：无标点时的自动提交长度阈值（默认：`12`）
  - `DIALOG_SOFT_FLUSH_LEN`：软标点提前提交的最小长度（默认：`20`）
  - `DIALOG_ENABLE_SOFT_SUBMIT`：是否启用软标点提前提交（默认：`true`）
  - `DIALOG_SILENCE_FLUSH_MS`：静默自动提交阈值（毫秒，默认：`1200`）。当识别文本在该时间内没有增长，视为一句话结束并提交。
- 行为说明：
  - ASR 识别文本按强标点自然断句提交到对话后端；若无强标点但达到软阈值，会按软标点在尾部切分提交；再无则按长度阈值兜底提交。
  - SSE 回复解析出纯文本后，先在服务端按上述策略组句，再以 `type=dialog` 的增量消息推送到前端；结束时发送一次 `stream=false` 的最终汇总。

SSE 模式请求示例（与后端联调一致）：

```
curl -N -X POST "http://0.0.0.0:8084/chat/stream" \
  -H "Accept: text/event-stream" \
  -H "Content-Type: application/json" \
  -H "X-Voice-Id: demo-voice" \
  -d '{"input":"请用简短语气问候一下我","sessionId":"session-demo"}'
```

配置集中到 `.env` 文件：复制 `.env.example` 为 `.env`，然后按需修改。

关键键：
- `ASR_HOST` / `ASR_PORT`：服务监听地址与端口
- `BACKEND_URL`：SAUC WebSocket 后端端点（默认 async）
- `APP_KEY` / `ACCESS_KEY`：凭据（必填）
- `RESOURCE_ID`：资源 ID（如 `volc.bigasr.sauc.duration`）
- `DIALOG_URL` / `DIALOG_MODE` / `VOICE_ID` / `SYSTEM_PROMPT`：对话后端配置
- `DIALOG_PUNCT_STRONG` / `DIALOG_PUNCT_SOFT` / `DIALOG_AUTOF_FLUSH_LEN` / `DIALOG_SOFT_FLUSH_LEN` / `DIALOG_ENABLE_SOFT_SUBMIT` / `DIALOG_SILENCE_FLUSH_MS`：断句与流式提交控制
 - `RESULT_DEDUP` / `RESULT_MIN_DELTA` / `RESULT_DEBOUNCE_MS`：ASR 增量去重与节流（减少重复文本）

说明：服务启动时自动加载 `.env`；前端可用查询参数临时覆盖（仅联调用）。

去重与节流行为：
- `RESULT_DEDUP=true`：当 `final=false` 且文本与上次相同，抑制输出（仍保留最终结果与对话桥更新）。
- `RESULT_MIN_DELTA`：变化字符数小于该阈值时抑制输出（默认 0 表示不启用）。
- `RESULT_DEBOUNCE_MS`：时间窗口内（毫秒）相同文本只保留一次（默认 0 表示不启用）。

### 前端消息协议

- 文本消息（JSON）：
  - `{"event":"end"}`：表示音频发送结束，服务端将发送最后一包并等待最终结果
  - `{"event":"ping"}`：心跳，服务端返回 `{"type":"pong"}`
- 二进制消息：
  - 直接发送 PCM16/16kHz/mono 原始字节（推荐每包 200ms 左右）。服务端将按到达顺序转发至 SAUC v3，并实时返回增量/最终结果。

### 服务端返回消息（JSON 文本）

- 结果消息：
  - `{ "type": "result", "session_id": "...", "sequence": <int>, "final": <bool>, "text": "...", "logid": "..." }`
  - `final=false` 表示增量结果；`final=true` 表示最终结果。
- 错误消息：
  - `{ "type": "error", "session_id": "...", "message": "..." }`

#### 音频消息（当启用语音克隆 TTS 时）

- 当对话后端返回 `reply` 时，服务端会将文本提交至语音克隆 TTS，并把返回的 MP3 音频以追加模式推送到前端：
  - `{ "type": "audio", "session_id": "...", "seq": <int>, "stream": true, "mime": "audio/mpeg", "sample_rate": 24000, "data_b64": "<Base64 MP3 bytes>" }`
  - `seq` 为服务端单调递增的分片编号；`data_b64` 是 MP3 字节的 Base64 编码。
  - 前端收到后请按 `seq` 追加播放；音频片段不包含序号，需以到达顺序组织。

### 语音克隆 TTS 集成

- 配置（均可在 `.env` 中设置或通过查询参数覆盖）：
  - `VOICE_CLONE_ENABLED`：是否启用 TTS（`true/false`）
  - `VOICE_CLONE_URL`：TTS HTTP 入口（推荐 `/api/tts/stream`，返回 `audio/mpeg`）
  - `VOICE_CLONE_MODE`：`stream`（单次文本直流，推荐）或 `segments_http`（预留）
  - `VOICE_CLONE_VOICE_TYPE`：说话人/风格标识（由 TTS 服务定义）
  - `VOICE_CLONE_SEND_FINAL_ONLY`：仅在最终回复触发 TTS（增量不触发）
  - `VOICE_CLONE_SAMPLE_RATE`：用于消息标注的采样率（MP3 默认 24kHz）
  - `VOICE_CLONE_SAVE_PATH` / `VOICE_CLONE_SAVE_MODE`：可选，透传给 TTS 服务侧保存（本服务不落盘原始音频）

- 运行示例（使用查询参数启用 TTS）：

```
ws://<host>:8081/ws-asr?
  dialog_url=http://0.0.0.0:8084/chat/stream&
  dialog_mode=sse&
  voice_id=demo-voice&
  voice_clone_enabled=true&
  voice_clone_url=http://0.0.0.0:8085/api/tts/stream&
  voice_clone_voice_type=demo
```

- 后端将以 `type=dialog` 推送文本回复；若启用 TTS，则并行以 `type=audio` 推送 MP3 片段（Base64）。

### 快速联调与运维命令（Linux）

- 初始化环境文件（幂等、带确认）：

```bash
# 进入项目目录
cd /srv/spark_asr

# 若不存在 .env，则从示例复制
if [ ! -f .env ]; then
  cp -v .env.example .env || { echo "复制 .env 失败"; exit 1; }
fi

# 写入必要的键（保留已有值），并启用语音克隆
grep -q '^APP_KEY=' .env || echo 'APP_KEY=' >> .env
grep -q '^ACCESS_KEY=' .env || echo 'ACCESS_KEY=' >> .env
grep -q '^RESOURCE_ID=' .env || echo 'RESOURCE_ID=volc.bigasr.sauc.duration' >> .env

# 设置/更新 TTS 相关键（覆盖写入）
sed -i 's/^VOICE_CLONE_ENABLED=.*/VOICE_CLONE_ENABLED=true/' .env
sed -i 's#^VOICE_CLONE_URL=.*#VOICE_CLONE_URL=http://0.0.0.0:8085/api/tts/stream#' .env
sed -i 's/^VOICE_CLONE_VOICE_TYPE=.*/VOICE_CLONE_VOICE_TYPE=demo/' .env

echo "当前 .env 生效的关键键："
grep -E '^(APP_KEY|ACCESS_KEY|RESOURCE_ID|VOICE_CLONE_)' .env | sed 's/.*/  &/'
```

- 启动服务并观察日志：

```bash
python sauc_asr_server.py --host 0.0.0.0 --port 8081
# 浏览器或前端连接：ws://<host>:8081/ws-asr?voice_clone_enabled=true&voice_clone_url=http://0.0.0.0:8085/api/tts/stream
```

- 会话日志目录：`./sessions/<YYYYMMDD>/<session_id>/`
  - `frontend_dialog.jsonl`：推送给前端的 `type=dialog` 文本消息
  - `frontend_audio.jsonl`：推送给前端的 `type=audio` 音频消息（Base64 片段和元信息）
  - `session.log`：会话级日志；`responses.jsonl`：ASR 后端响应记录

### 前端示例（浏览器）

以下示例展示如何使用 WebAudio 将麦克风采集的音频转换为 PCM16 并通过 WebSocket 发送到服务端（示意代码，需根据实际环境调整）：

```javascript
const ws = new WebSocket('ws://localhost:8081/ws-asr');
ws.onmessage = (evt) => {
  const msg = JSON.parse(evt.data);
  if (msg.type === 'result') {
    console.log(msg.final ? 'Final:' : 'Partial:', msg.text);
  } else if (msg.type === 'error') {
    console.error('Error:', msg.message);
  }
};

const audioCtx = new (window.AudioContext || window.webkitAudioContext)({ sampleRate: 16000 });
navigator.mediaDevices.getUserMedia({ audio: true }).then(stream => {
  const source = audioCtx.createMediaStreamSource(stream);
  const processor = audioCtx.createScriptProcessor(4096, 1, 1);
  source.connect(processor);
  processor.connect(audioCtx.destination);
  processor.onaudioprocess = (e) => {
    const input = e.inputBuffer.getChannelData(0); // Float32Array [-1.0, 1.0]
    // 转换为 PCM16 Little-Endian
    const pcm16 = new Int16Array(input.length);
    for (let i = 0; i < input.length; i++) {
      let s = Math.max(-1, Math.min(1, input[i]));
      pcm16[i] = s < 0 ? s * 0x8000 : s * 0x7FFF;
    }
    ws.send(new Blob([pcm16.buffer]));
  };
});

// 结束时：
// ws.send(JSON.stringify({ event: 'end' }));
```

### 重要说明

- 后端桥接默认使用 SAUC v3 优化双向流式接口 `bigmodel_async`；若握手失败会自动回退到 `bigmodel_nostream`。
- 完整请求使用 JSON+GZIP，音频分包使用 NO_SERIALIZATION+GZIP。
- 音频格式要求：PCM16/16kHz/mono 原始数据。如果前端只能输出其它格式，请在前端完成转码或在后端增加流式转码逻辑（复杂度较高）。
- 分包建议：200ms 左右，有利于兼顾实时性与识别稳定性。
- 每个连接会生成唯一 `session_id` 并独立保存日志，便于问题追踪与统计。
 - 若缺少 `app_key/access_key/resource_id`，服务端会在握手前返回友好错误提示。

- 客户端完整请求（JSON + GZIP 压缩）先行发送，随后发送音频分包。
- 仅发送 WAV 的 data 子块（纯音频数据），不包含文件头，兼容性更佳。
- `--seg-duration` 默认 200ms，建议保持该值以兼顾实时性与准确性。
- 最后一包按照协议使用「负序列号 + NEG_WITH_SEQUENCE」标记，以触发服务端返回最终结果。

---

## 常见问题排查

- 连接失败：
  - 检查 `X-Api-App-Key` 与 `X-Api-Access-Key` 是否正确、是否在对应项目下开通了资源。
  - 确认 `Resource ID` 与所购版本一致（小时版/并发版）。
  - 查看 `run.log`，记录的 `X-Tt-Logid` 可用于联系平台排查。
- 识别结果为空或错误：
  - 确认音频质量、采样率与声道数是否符合要求（脚本会自动转码为 16kHz/mono/PCM16）。
  - 尝试将分包时长设为 200ms。
  - 确认是否正确发送了最后一包（负序列包）。
- ffmpeg 未找到：请确保其已安装并在 PATH 中可执行，执行 `ffmpeg -version` 验证。

---

## 安全建议

- 切勿将密钥提交到公共仓库。
- 生产环境请通过环境变量或密钥管理服务加载凭据，例如：
  - 在 Linux 中设置（示例）：
    ```bash
    export APP_KEY="<你的APP_KEY>"
    export ACCESS_KEY="<你的ACCESS_KEY>"
    export RESOURCE_ID="volc.bigasr.sauc.duration"
    python sauc_asr_server.py --host 0.0.0.0 --port 8081
    ```
  - 在 PowerShell 中设置：
    ```powershell
    $env:APP_KEY="5142285262"
    $env:ACCESS_KEY="3hpVlzSZZkLakcOEMsfKDcDDWWdKCxpb"
    $env:SECRET_KEY="XvE8oA0RSecmzf5RB45Ln3eTvNQFDT8b"
    ```
  - 在脚本中读取并覆盖默认配置。

---

## 免责声明

本示例脚本用于演示与开发调试，实际生产环境请做好异常重试、超时控制、日志脱敏、并发控制、以及密钥的安全管理。
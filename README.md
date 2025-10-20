# Volcengine SAUC v3 大模型流式语音识别 Demo（Windows）

本仓库包含 `sauc_websocket_demo.py` 脚本，用于在 Windows 环境下调用火山引擎开放平台的「大模型流式 ASR」WebSocket API。已为你集成以下配置：
- APP ID（App Key）：5142285262
- Access Token（Access Key）：3hpVlzSZZkLakcOEMsfKDcDDWWdKCxpb
- Secret Key：XvE8oA0RSecmzf5RB45Ln3eTvNQFDT8b（当前协议未用到，保留）

脚本默认使用「最高准确率」的接口模式：`bigmodel_nostream`（流式输入模式：输入音频>15s或发送最后一包后返回结果，准确率更高）。

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

- 默认接口 URL：`wss://openspeech.bytedance.com/api/v3/sauc/bigmodel_nostream`（最高准确率）
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

本脚本默认使用 `bigmodel_nostream` 并在完整请求 payload 中启用：
- ITN（数字归一化）：`enable_itn=True`
- 标点：`enable_punc=True`
- DDC（脏词清理）：`enable_ddc=True`
- 非双向流式模式标记：`enable_nonstream=True`

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

## 发送协议与分包策略

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
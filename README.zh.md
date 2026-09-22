![screenshot](docs/screenshot.png)

# interactive-mcp

[![lint](https://github.com/coldcat8120/interactive-mcp/actions/workflows/lint.yml/badge.svg)](https://github.com/coldcat8120/interactive-mcp/actions/workflows/lint.yml)
[![License: MIT](https://img.shields.io/badge/License-MIT-yellow.svg)](https://opensource.org/licenses/MIT)

**中文** | [English](README.md)

让任何支持 MCP 的 Agent 框架，**主动弹出一个板书讲解窗口**——用户在独立窗口里边看板书边提问，全程只用一个 LLM。

## 这是什么

一个通用 MCP 技能，提供"陪读学长"式的板书讲解体验：

- **Agent 主动弹窗**：工具被调用后，独立浏览器窗口自动打开
- **SVG 黑板**：LLM 生成 SVG，前端渲染为逐步书写的板书
- **单一 LLM**：没有任何"第二个大脑"，全部推理由调用方框架完成
- **LaTeX 渲染**：黑板和对话区都支持数学公式（KaTeX）
- **流式书写 + 可调速**：板书逐块出现，速度可调
- **语音输入 / 朗读**（可选）：ASR / TTS 用户自行配置，未配置时自动降级

## 工作原理

```
┌─────────────────────────────┐
│  通用 Agent 框架             │
│  (OpenCode / QwenPaw / ...) │
└──────────┬──────────────────┘
           │ MCP tool call
           ▼
┌─────────────────────────────┐
│  interactive-mcp (MCP)      │
│  ┌─────────────────────┐    │
│  │ Broker (aiohttp)    │    │
│  │  - 弹窗 + WebSocket │    │
│  │  - 流式推送          │    │
│  │  - ASR / TTS 代理    │    │
│  └─────────────────────┘    │
└──────────┬──────────────────┘
           │ WebSocket
           ▼
┌─────────────────────────────┐
│  弹出的板书窗口               │
│  - SVG 黑板                  │
│  - 对话区                    │
│  - LaTeX + TTS + ASR         │
└─────────────────────────────┘
```

三个 MCP 工具串成一个循环：

```
open_tutoring(topic)
    ↓
show_and_wait(content)  ← 推送板书 + 阻塞等用户输入
    ↓  循环
show_and_wait(content)
    ↓
close_tutoring()
```

**关键设计**：MCP 工具的阻塞特性被用来"暂停"Agent 主循环，用户在弹窗内的输入作为工具返回值喂回给 Agent。所以全程只有一个 LLM——就是调用方框架的。

## 安装

### 1. 克隆并安装依赖

```bash
git clone https://github.com/coldcat8120/interactive-mcp.git
cd interactive-mcp
pip install -r requirements.txt
```

### 2. 复制 KaTeX 资源

项目需要 `static/katex/` 目录下的 KaTeX 资源。从 [KaTeX 官网](https://katex.org/) 下载后解压到该目录。

### 3. 在 Agent 框架中配置 MCP

以 **OpenCode** 为例（`~/.config/opencode/opencode.jsonc`）：

```jsonc
{
  "mcp": {
    "my-mcp": {
      "type": "local",
      "command": [
        "python",
        "/绝对路径/interactive-mcp/server.py"
      ],
      "timeout": 3600000
    }
  }
}
```

**注意 timeout 必须设大**（如 1 小时），因为工具是阻塞式的，会等用户在新窗口里交互完才返回。

其他框架同理——任何支持 stdio 传输的 MCP 客户端都能直接连接。

## 使用

在 Agent 对话里说：

> 调用 my-mcp 的 open_tutoring 工具，主题：勾股定理

几秒后浏览器弹出新窗口，讲解 Agent 开始板书。

## 配置（可选）

窗口右上角点击 **`⚙️ 语音`** 展开设置面板，内含 ASR 和 TTS 两组配置。所有配置仅存于浏览器 localStorage，**未配置时功能会自动降级，不会崩溃**。

### 🎤 ASR — 语音输入

填入任意 OpenAI 兼容的 `/audio/transcriptions` 端点：

| 服务 | URL | 模型 |
|---|---|---|
| 本地 llama.cpp + Qwen3-ASR | `http://127.0.0.1:8082` | `qwen3-asr` |
| OpenAI Whisper | `https://api.openai.com/v1` | `whisper-1` |
| 硅基流动 / Groq | 各家 base_url | 各家模型 ID |

本地 Qwen3-ASR 部署（llama.cpp）：

```bash
llama-server \
  -m Qwen3-ASR-0.6B-Q8_0.gguf \
  --mmproj mmproj-Qwen3-ASR-0.6B-bf16.gguf \
  --port 8082
```

**Broker 会用 ffmpeg 把浏览器录音转成 16kHz 单声道 WAV 再转发**，所以需要系统装了 ffmpeg（Windows 上 `ffprobe` 单独存在也可以）。

未配置时点 🎤 只会提示并展开设置面板，不会崩溃。

### 🔊 TTS — 语音朗读

填入任意 OpenAI 兼容的 `/audio/speech` 端点：

| 服务 | URL | 模型 | 音色 |
|---|---|---|---|
| OpenAI | `https://api.openai.com/v1` | `tts-1` | `alloy` / `nova` / … |
| 硅基流动 | `https://api.siliconflow.cn/v1` | `FunAudioLLM/CosyVoice2-0.5B` | `claire` / … |
| 本地 TTS | `http://127.0.0.1:8083` | 依服务而定 | 依服务而定 |

**留空则使用浏览器内置 SpeechSynthesis**（音色有限但无需配置）。

配置了服务端 TTS 时优先使用服务端；服务端失败会自动回退浏览器，讲解不中断。

## 设计哲学

### 为什么是"通用 Agent 驱动特化技能"，而不是"特化 Agent"？

传统做法是把整个板书讲解 Agent 打包成一个独立进程，里面再养一个 LLM。问题：

1. **两个大脑**——上下文断裂，特化 Agent 看不到主框架的对话历史
2. **prompt 膨胀**——通用框架的 prompt 里要塞进教学法、SVG 规范等
3. **不可复用**——换个框架就要重写

本项目的做法：**MCP Server 只做窗口和管道，LLM 推理全部交给调用方**。

- 单一 LLM，上下文完整
- 教学约束通过工具描述承载，不污染框架 prompt
- 任何支持 MCP 的框架即插即用

### 为什么板书用 SVG？

LLM 生成结构化图形时，SVG 是**唯一同时满足**以下条件的格式：

- LLM 熟悉度最高（训练数据里大量存在）
- 浏览器原生渲染，无需额外引擎
- 可通过 `foreignObject` 嵌入 KaTeX 数学公式
- 文本内容可被剥离出来做 TTS

## 已知限制

- **书写与朗读无法完全同步**：朗读约 5 字/秒，书写即使最慢也有 100+ 字符/秒。目前通过"默认放慢书写"来缓解
- **SVG 内的 `<text>` 内容延迟朗读**：等 `</text>` 闭合后才读，不是字符级流式
- **浏览器 TTS 音色受限**：仅有系统内置音色，效果因系统而异。建议配置服务端 TTS
- **KaTeX 对 CJK 有限制**：公式内的中文需 `\text{}` 包裹，前端已做自动兜底

## 目录结构

```
interactive-mcp/
├── server.py              # MCP 工具定义（三工具循环）
├── broker.py              # 常驻 Broker：窗口 + WebSocket + 流式 + ASR/TTS 代理
├── requirements.txt       # mcp + aiohttp + httpx
├── LICENSE                # MIT
├── NOTICE                 # 第三方组件许可（KaTeX 等）
├── docs/
│   └── screenshot.png     # 截图
└── static/
    ├── index.html         # 板书 + 对话 + KaTeX + TTS + ASR + 速度滑块
    └── katex/             # KaTeX 资源
```

## 扩展方向

当前骨架的"弹窗 + 阻塞循环 + 结构化输出"是个通用范式。把 SVG 黑板换成别的东西就是新技能：

- **Mermaid 讲解**：流程图、时序图教学
- **代码编辑器**：实时编写并运行代码
- **3D 场景**：几何、物理可视化
- **思维导图**：知识结构逐步展开

## 致谢

灵感来自一块真实的黑板——老师一边写一边讲，学生一边看一边问。数字时代不该丢掉这种节奏。

## License

本项目采用 [MIT License](LICENSE)。

第三方组件的许可信息见 [NOTICE](NOTICE)（含 KaTeX 等）。

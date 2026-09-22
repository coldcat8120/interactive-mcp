"""
MCP Server 入口。

三个工具串成一个循环：
    open_tutoring(topic) → show_and_wait(content)* → close_tutoring()

LLM 每轮生成回复后，通过 show_and_wait 推送到窗口，并等待用户下一条输入。
"""

import asyncio
import sys
import uuid
import webbrowser

from mcp.server.fastmcp import FastMCP

from broker import BrokerSession


def log(msg: str) -> None:
    print(f"[MCP] {msg}", file=sys.stderr, flush=True)


mcp = FastMCP("tutor")

_state = {"session": None}


@mcp.tool()
async def open_tutoring(topic: str) -> str:
    """打开一个独立的板书讲解窗口。窗口是用户后续所有交互的载体：
    用户在其中输入问题，你的回复也显示在其中——SVG 部分会渲染为黑板板书。

    调用后立即返回。你必须紧接着调用 show_and_wait 推送开场讲解。

    Args:
        topic: 要讲解的主题，例如"勾股定理"、"一元二次方程"。
    """
    old = _state["session"]
    if old is not None:
        await old.stop()
        _state["session"] = None

    session = BrokerSession(uuid.uuid4().hex[:8], topic)
    url = await session.start()
    _state["session"] = session
    await asyncio.to_thread(webbrowser.open, url)
    log(f"opened session for topic={topic!r}")

    return (
        f"窗口已打开，主题：{topic}。\n"
        f"请立即调用 show_and_wait，推送你的开场讲解——"
        f"必须包含一段完整的 SVG 板书（viewBox=\"0 0 1080 960\"，以 <svg> 开头、</svg> 结尾），"
        f"以及一两句简短的引导文字。"
    )


@mcp.tool()
async def show_and_wait(content: str) -> str:
    """把你的回复推送到板书窗口，然后阻塞等待用户的下一条消息。

    【黑板画布信息】
    你输出的 SVG 会被直接渲染在一块真实画布上，画布参数如下：
      · viewBox: "0 0 1080 960"（宽 1080，高 960）
      · 背景色：#173d23（深绿黑板色），已由前端提供
      · 画布边界之外的内容会被裁切，请合理安排元素位置与尺寸
    请根据以上信息自行决定文字大小、颜色、行距、排版方式——
    深色背景配浅色元素，长文本请主动分行。你不必遵循固定的样式模板。

    【渲染能力】
    · 支持 LaTeX 数学公式：使用 $...$ 表示行内公式、$$...$$ 表示块级公式，
      也支持 \\(...\\) 和 \\[...\\] 写法。
      SVG 内的 <text>/<tspan> 和对话区的文字都会自动渲染为数学排版。
     · **LaTeX 公式里如果出现中文，必须用 \\text{} 包裹**，
      例如 \\sqrt{\\text{面积}} 而不是 \\sqrt{面积}；
      a_{\\text{鸡}} 而不是 a_鸡。否则根号、分数线等符号的宽度会算错。
    · 对话区的纯文本会被自动朗读（浏览器 TTS），因此文字要通顺、
      数学公式请用规范 LaTeX（如 $\\sqrt{2}$ 会被朗读为"根号二"）。
    · 使用 HTML 标签（如 <div>、<span>）没有意义，会被自动清除。

    【架构约束（LLM 无法推断，必须遵守）】
    SVG 中严禁用 <rect> 或 <path> 填充大面积背景色，
    黑板底色已由前端提供，你再画一层会遮挡它。

    【教学方法 —— 严格遵守】
    1. 苏格拉底式引导：**严禁在板书或文字中给出最终答案**。
       你只能引导学生自己得出答案，可以给出算式结构（如 "35 × 2 = ?"），
       但**绝对不能写出等号后的具体数值**。学生答对后也不要复述答案，
       而是用"没错，那下一步呢？"这样的方式推进。
    2. 每轮只聚焦一个核心问题，等用户回应后再进入下一步。
    3. SVG 之外只配 1-2 句简短的引导语，不要长篇讲解。

    【循环协议 —— 严格遵守】
    调用后会等待用户在窗口中输入。返回值是用户的下一条消息。

      · 如果返回 "__SESSION_ENDED__"，说明用户结束了会话。
        你必须**立即调用 close_tutoring** 来收尾。
        **绝对不要**在收到此标记后再次调用 show_and_wait。

      · 否则，请分析用户消息、生成新的回复，再次调用 show_and_wait。
        不要中途停下——对话控制权在工具里，你必须持续循环。

    Args:
        content: 你要推送给用户的内容（含 SVG 板书与文字）。
    """
    session = _state["session"]
    if session is None:
        return "错误：没有活跃的会话，请先调用 open_tutoring。"

    if session.ended:
        log("show_and_wait called on ended session, returning immediately")
        return "__SESSION_ENDED__ —— 请立即调用 close_tutoring。"

    await session.push_reply(content)
    log(f"pushed reply ({len(content)} chars)")

    user_msg = await session.wait_for_user()
    if user_msg is None:
        log("session ended")
        return "__SESSION_ENDED__ —— 请立即调用 close_tutoring。"

    log(f"got user msg: {user_msg!r}")
    return (
        f"用户说：{user_msg}\n\n"
        f"请生成你的回复（含更新后的 SVG 板书与文字），然后再次调用 show_and_wait 推送。"
    )


@mcp.tool()
async def close_tutoring() -> str:
    """关闭板书窗口，结束整个讲解会话。"""
    session = _state["session"]
    if session is None:
        return "没有活跃的会话。"
    await session.stop()
    _state["session"] = None
    log("session closed")
    return "讲解会话已结束。"


if __name__ == "__main__":
    mcp.run()
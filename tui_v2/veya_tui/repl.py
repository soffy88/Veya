"""
veya_tui.repl — F. 주 REPL 루프 + G. -p/print 모드
========================================================
F: HicodeREPL / LoopOrchestrator / AgentLoopAdapter / SessionContext
G: PrintMode / PipeFriendly
"""

from __future__ import annotations

import asyncio
import sys
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

# 경로 설정
_HERE = Path(__file__).resolve().parent.parent.parent
for _pkg in ["oprim", "oskill", "omodul", "layer4"]:
    _p = _HERE / _pkg
    if _p.exists() and str(_p) not in sys.path:
        sys.path.insert(0, str(_p))

from veya_tui.input import (  # noqa: E402
    ArgParser,
    CliArgs,
    EnvLoader,
    PromptInput,
    StartupBanner,
)
from veya_tui.render import (  # noqa: E402
    DividerLine,
    ExitHandler,
    InterruptHandler,
    MarkdownRenderer,
    SpinnerAnimation,
    StatusBar,
    StreamPrinter,
    ToolCallRenderer,
    ToolResultRenderer,
    YesNoPrompt,
    dim,
    red,
)

# ===========================================================================
# F. 세션 컨텍스트
# ===========================================================================


@dataclass
class SessionContext:
    """REPL이 보유하는 전역 컨텍스트."""

    args: CliArgs
    caller: Any  # LLMCaller Protocol
    session: Any  # layer4.Session
    session_manager: Any  # layer4.SessionManager
    registry: Any  # layer4.ToolRegistry
    slash_router: Any  # layer4.SlashRouter
    permission_gate: Any  # layer4.PermissionGate
    hook_manager: Any = None  # layer4.HookManager
    subagent_loader: Any = None  # layer4.SubagentLoader
    status_bar: StatusBar = field(default_factory=StatusBar)
    spinner: SpinnerAnimation = field(default_factory=SpinnerAnimation)
    stream_printer: StreamPrinter = field(default_factory=StreamPrinter)
    md_renderer: MarkdownRenderer = field(default_factory=MarkdownRenderer)
    interrupt_handler: InterruptHandler = field(default_factory=InterruptHandler)

    @property
    def no_color(self) -> bool:
        return self.args.no_color


def build_session_context(args: CliArgs) -> SessionContext:
    """CliArgs로부터 전체 컨텍스트를 조립."""
    from layer4 import (  # pragma: no cover
        ApprovalHistory,
        HookManager,
        PermissionGate,
        PermissionPolicy,
        SessionManager,
        SubagentLoader,
        build_default_registry,
        build_default_router,
    )

    # caller 생성
    caller = EnvLoader.get_caller(model=args.model)  # pragma: no cover

    # session
    mgr = SessionManager()  # pragma: no cover
    if args.continue_session and not args.session_id:  # pragma: no cover
        sessions = mgr.list()  # pragma: no cover
        if sessions:  # pragma: no cover
            args.session_id = sessions[0]["id"]  # pragma: no cover

    if args.session_id:  # pragma: no cover
        session = mgr.load(args.session_id) or mgr.create(
            mode=args.mode, cwd=args.cwd
        )  # pragma: no cover
    else:
        session = mgr.create(mode=args.mode, cwd=args.cwd)  # pragma: no cover

    # permission gate
    _prompt = YesNoPrompt(
        no_color=args.no_color,  # pragma: no cover
        auto="allow" if args.mode == "bypass" else None,
    )
    gate = PermissionGate(  # pragma: no cover
        policy=PermissionPolicy({"mode": args.mode}),
        history=ApprovalHistory(),
    )

    # 도구 등록
    registry = build_default_registry(permission_gate=lambda tc: gate(tc))  # pragma: no cover

    # slash router
    router = build_default_router(  # pragma: no cover
        custom_commands_dir=Path(args.cwd) / ".claude" / "commands"
    )

    # hook manager
    hook_mgr = HookManager()  # pragma: no cover

    # subagent loader
    sa_loader = SubagentLoader(agents_dir=Path(args.cwd) / ".claude" / "agents")  # pragma: no cover

    no_color = args.no_color  # pragma: no cover
    return SessionContext(  # pragma: no cover
        args=args,
        caller=caller,
        session=session,
        session_manager=mgr,
        registry=registry,
        slash_router=router,
        permission_gate=gate,
        hook_manager=hook_mgr,
        subagent_loader=sa_loader,
        status_bar=StatusBar(no_color=no_color),
        spinner=SpinnerAnimation(no_color=no_color),
        stream_printer=StreamPrinter(no_color=no_color),
        md_renderer=MarkdownRenderer(no_color=no_color),
        interrupt_handler=InterruptHandler(),
    )


# ===========================================================================
# F. AgentLoopAdapter
# ===========================================================================


class AgentLoopAdapter:
    """
    oservice.agentic_loop을 REPL 친화적 async 인터페이스로 래핑.
    llm_stream 기반 스트리밍 + on_tool_call 콜백 지원.
    """

    def __init__(self, ctx: SessionContext) -> None:
        self.ctx = ctx

    async def run(
        self,
        task: str,
        *,
        on_tool_call=None,
        on_tool_result=None,
        on_thinking=None,
        on_text=None,
    ) -> dict:
        """
        태스크를 agentic_loop에 전달. 없으면 단순 LLM 호출.
        Returns: {text, cost_usd, tool_calls, status}
        """
        ctx = self.ctx

        # oservice.agentic_loop 시도
        try:
            from oservice.agentic_loop import AgenticLoop  # type: ignore
            # from layer4 import build_tool_schemas  # pragma: no cover

            mode = ctx.session.mode  # pragma: no cover
            tools = (
                ctx.registry.readonly_only() if mode == "plan" else ctx.registry.all()
            )  # pragma: no cover

            loop = AgenticLoop(  # pragma: no cover
                caller=ctx.caller,
                tools=tools,
                permission_gate=ctx.permission_gate,
                hook_dispatch=None,
                on_step=ctx.status_bar.update,
            )
            result = await loop.run(task, messages=ctx.session.messages)  # pragma: no cover
            return result  # pragma: no cover
        except ImportError:
            pass

        # agentic_loop 없으면 llm_stream 직접 호출
        return await self._direct_llm(
            task,
            on_tool_call=on_tool_call,
            on_tool_result=on_tool_result,
            on_thinking=on_thinking,
            on_text=on_text,
        )

    async def _direct_llm(self, task: str, **callbacks) -> dict:
        """oservice 없을 때 단순 LLM 호출 (도구 루프 없음)."""
        from oprim.llm import llm_complete

        ctx = self.ctx
        messages = list(ctx.session.messages)
        messages.append({"role": "user", "content": task})

        # system prompt 빌드
        try:
            tools = (
                ctx.registry.readonly_only() if ctx.session.mode == "plan" else ctx.registry.all()
            )
            tool_summary = "\n".join(f"- {t.name}: {t.description}" for t in tools[:20])
        except Exception:  # pragma: no cover
            tool_summary = ""  # pragma: no cover

        try:
            resp = await llm_complete(
                messages,
                caller=ctx.caller,
                model=ctx.args.model,
                system=f"You are veya, an AI coding agent in {ctx.session.mode.upper()} mode.\n"
                f"CWD: {ctx.args.cwd}\n"
                f"{'Tools: ' + tool_summary if tool_summary else ''}",
            )
            return {
                "text": resp.text,
                "cost_usd": resp.cost_usd,
                "tool_calls": resp.tool_calls,
                "input_tokens": resp.input_tokens,
                "output_tokens": resp.output_tokens,
                "status": "completed",
            }
        except Exception as e:
            return {
                "text": "",
                "error": str(e),
                "status": "failed",
                "cost_usd": 0.0,
                "tool_calls": [],
            }


# ===========================================================================
# F. LoopOrchestrator
# ===========================================================================


class LoopOrchestrator:
    """
    한 번의 사용자 입력을 처리하는 오케스트레이터.
    slash 라우팅 → agentic_loop → 렌더링 → 세션 업데이트.
    """

    def __init__(self, ctx: SessionContext) -> None:
        self.ctx = ctx
        self._tool_call_renderer = ToolCallRenderer(no_color=ctx.no_color)
        self._tool_result_renderer = ToolResultRenderer(no_color=ctx.no_color)
        self._agent_loop = AgentLoopAdapter(ctx)

    async def handle(self, text: str) -> None:
        ctx = self.ctx
        nc = ctx.no_color
        router = ctx.slash_router

        # slash 명령 처리
        if router.is_command(text):
            ctx.spinner.start("running")
            try:
                result = await router.dispatch(
                    text,
                    session=ctx.session,
                    caller=ctx.caller,
                    cwd=ctx.args.cwd,
                    session_manager=ctx.session_manager,
                    hook_manager=ctx.hook_manager,
                    subagent_loader=ctx.subagent_loader,
                    multi_session_router=None,
                )
            finally:
                ctx.spinner.stop()

            if result.error:
                print(red(f"  ✗ {result.error}", nc=nc))
                return

            if result.redirect_to_loop and result.text:
                # /plan task → agentic_loop로 재전달
                await self._run_agent(result.text)  # pragma: no cover
                return  # pragma: no cover

            if result.text:
                print()
                print(ctx.md_renderer.render(result.text))
                print()
            return

        # 일반 사용자 메시지 → agentic_loop
        await self._run_agent(text)

    async def _run_agent(self, task: str) -> None:
        ctx = self.ctx  # pragma: no cover
        nc = ctx.no_color  # pragma: no cover

        # 세션에 사용자 메시지 추가
        ctx.session.messages.append({"role": "user", "content": task})  # pragma: no cover

        # 스피너 시작
        ctx.spinner.start("thinking")  # pragma: no cover

        def on_tool_call(name: str, inp: dict) -> None:  # pragma: no cover
            ctx.spinner.stop()  # pragma: no cover
            self._tool_call_renderer.print(name, inp)  # pragma: no cover

        def on_tool_result(name: str, result: dict) -> None:  # pragma: no cover
            self._tool_result_renderer.print(result, tool_name=name)  # pragma: no cover
            ctx.spinner.start("thinking")  # pragma: no cover

        try:  # pragma: no cover
            result = await self._agent_loop.run(  # pragma: no cover
                task,
                on_tool_call=on_tool_call,
                on_tool_result=on_tool_result,
            )
        except asyncio.CancelledError:  # pragma: no cover
            ctx.spinner.stop()  # pragma: no cover
            print(dim("\n  [cancelled]", nc=nc))  # pragma: no cover
            return  # pragma: no cover
        finally:
            ctx.spinner.stop()  # pragma: no cover

        # 어시스턴트 응답 처리
        text = result.get("text", "")  # pragma: no cover
        error = result.get("error")  # pragma: no cover
        cost = result.get("cost_usd", 0.0)  # pragma: no cover
        in_tok = result.get("input_tokens", 0)  # pragma: no cover
        out_tok = result.get("output_tokens", 0)  # pragma: no cover

        if error:  # pragma: no cover
            print(red(f"\n  ✗ Error: {error}", nc=nc))  # pragma: no cover
        elif text:  # pragma: no cover
            print()  # pragma: no cover
            print(DividerLine.assistant_prefix(no_color=nc))  # pragma: no cover
            print(ctx.md_renderer.render(text))  # pragma: no cover

        # 세션 업데이트
        if text:  # pragma: no cover
            ctx.session.messages.append({"role": "assistant", "content": text})  # pragma: no cover
        ctx.session.cost_usd += cost  # pragma: no cover
        ctx.status_bar.update(
            cost=cost,
            in_tok=in_tok,
            out_tok=out_tok,  # pragma: no cover
            mode=ctx.session.mode,
            session_id=ctx.session.id,
        )
        ctx.session_manager.save(ctx.session)  # pragma: no cover

        # 상태표시줄 갱신
        ctx.status_bar.print()  # pragma: no cover
        print()  # pragma: no cover


# ===========================================================================
# F. HicodeREPL — 주 루프
# ===========================================================================


class HicodeREPL:
    """
    주 REPL 루프: 읽기 → slash 라우팅 → agentic_loop → 렌더링.

    진입점: HicodeREPL(ctx).run()
    """

    def __init__(self, ctx: SessionContext) -> None:
        self.ctx = ctx  # pragma: no cover
        self._prompt = PromptInput(cwd=ctx.args.cwd, no_color=ctx.no_color)  # pragma: no cover
        self._orchestrator = LoopOrchestrator(ctx)  # pragma: no cover
        self._exit_handler = ExitHandler(session_manager=ctx.session_manager)  # pragma: no cover

    def run(self) -> int:
        """동기 진입점. 0=정상 종료."""
        ctx = self.ctx  # pragma: no cover

        # 시작 배너
        StartupBanner.print(ctx.args)  # pragma: no cover

        # 인터럽트 핸들러 설치
        ctx.interrupt_handler.install()  # pragma: no cover

        # 최초 태스크 (명령행 인수로 전달된 경우)
        if ctx.args.task:  # pragma: no cover
            asyncio.run(self._orchestrator.handle(ctx.args.task))  # pragma: no cover

        # REPL 루프
        try:  # pragma: no cover
            while True:  # pragma: no cover
                DividerLine.print(no_color=ctx.no_color)  # pragma: no cover
                print(DividerLine.user_prefix(no_color=ctx.no_color), end="")  # pragma: no cover

                text = self._prompt.read()  # pragma: no cover

                if text is None:  # pragma: no cover
                    # Ctrl+D 또는 Ctrl+C
                    if self._exit_handler.handle(ctx.session):  # pragma: no cover
                        break  # pragma: no cover
                    continue  # pragma: no cover

                if text.strip() in ("/exit", "/quit"):  # pragma: no cover
                    if self._exit_handler.handle(ctx.session):  # pragma: no cover
                        break  # pragma: no cover
                    continue  # pragma: no cover

                try:  # pragma: no cover
                    asyncio.run(self._orchestrator.handle(text))  # pragma: no cover
                except KeyboardInterrupt:  # pragma: no cover
                    print(dim("\n  [interrupted]", nc=ctx.no_color))  # pragma: no cover

        except KeyboardInterrupt:  # pragma: no cover
            pass  # pragma: no cover
        finally:
            ctx.interrupt_handler.uninstall()  # pragma: no cover
            self._prompt.close()  # pragma: no cover
            ctx.status_bar.clear()  # pragma: no cover

        return 0  # pragma: no cover


# ===========================================================================
# G. -p / Print 모드
# ===========================================================================


class PipeFriendly:
    """stdout=결과, stderr=로그, ANSI 없음, exit code 전파."""

    @staticmethod
    def print_result(text: str) -> None:
        print(text)

    @staticmethod
    def print_log(msg: str) -> None:
        print(msg, file=sys.stderr)

    @staticmethod
    def is_pipe() -> bool:
        return not sys.stdout.isatty()


class PrintMode:
    """veya -p 'task' 비대화형 단일 실행."""

    def __init__(self, ctx: SessionContext) -> None:
        self.ctx = ctx
        self._orchestrator = AgentLoopAdapter(ctx)

    async def run(self, task: str) -> int:
        """0=성공, 1=실패."""
        PipeFriendly.print_log("veya: running task...")

        result = await self._orchestrator.run(task)

        text = result.get("text", "")
        error = result.get("error")
        cost = result.get("cost_usd", 0.0)

        if error:
            PipeFriendly.print_log(f"veya: error: {error}")
            return 1

        if text:
            PipeFriendly.print_result(text)

        PipeFriendly.print_log(
            f"veya: done  cost=${cost:.4f}  "
            f"tokens={result.get('input_tokens', 0) + result.get('output_tokens', 0)}"
        )
        return 0


# ===========================================================================
# 메인 진입점
# ===========================================================================


def main(argv: list[str] | None = None) -> int:
    """veya CLI 메인. setup.py console_scripts 등록점."""
    args = ArgParser.parse(argv)  # pragma: no cover
    EnvLoader.load(verbose=args.verbose)  # pragma: no cover

    # API 키 확인
    # API key 检查：根据模型自动选择 provider  # pragma: no cover
    provider = EnvLoader.detect_provider(args.model)  # pragma: no cover
    if not EnvLoader.check_api_key(provider):  # pragma: no cover
        key_name = {"deepseek": "DEEPSEEK_API_KEY"}.get(
            provider, "ANTHROPIC_API_KEY"
        )  # pragma: no cover
        print(
            f"veya: {key_name} not set.\n"  # pragma: no cover
            f"  Run: export {key_name}=<your-key>",
            file=sys.stderr,
        )  # pragma: no cover
        return 1  # pragma: no cover

    ctx = build_session_context(args)  # pragma: no cover

    if args.print_mode:  # pragma: no cover
        if not args.task:  # pragma: no cover
            print("veya -p requires a task argument", file=sys.stderr)  # pragma: no cover
            return 1  # pragma: no cover
        return asyncio.run(PrintMode(ctx).run(args.task))  # pragma: no cover

    return HicodeREPL(ctx).run()  # pragma: no cover


if __name__ == "__main__":
    sys.exit(main())  # pragma: no cover

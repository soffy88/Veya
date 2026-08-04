"""
veya_tui.render — C. 출력 렌더러 + D. 상태표시줄 + E. 인터랙션 컨트롤
=========================================================================
C: MarkdownRenderer / CodeBlockRenderer / ToolCallRenderer /
   ToolResultRenderer / ThinkingBlock / StreamPrinter
D: StatusBar / LayoutManager / SpinnerAnimation / DividerLine
E: InterruptHandler / ExitHandler / YesNoPrompt / PagerView / CopyHint
"""

from __future__ import annotations

import asyncio
import itertools
import shutil
import signal
import sys
import textwrap
import threading
import time
from collections.abc import Iterator
from contextlib import contextmanager
from typing import Any

# ---------------------------------------------------------------------------
# 색상 유틸
# ---------------------------------------------------------------------------


def _ansi(code: str, text: str, *, nc: bool = False) -> str:
    return text if nc else f"\033[{code}m{text}\033[0m"


def bold(t: str, *, nc: bool = False) -> str:
    return _ansi("1", t, nc=nc)


def dim(t: str, *, nc: bool = False) -> str:
    return _ansi("2", t, nc=nc)


def green(t: str, *, nc: bool = False) -> str:
    return _ansi("32", t, nc=nc)


def cyan(t: str, *, nc: bool = False) -> str:
    return _ansi("36", t, nc=nc)


def yellow(t: str, *, nc: bool = False) -> str:
    return _ansi("33", t, nc=nc)


def red(t: str, *, nc: bool = False) -> str:
    return _ansi("31", t, nc=nc)


def gray(t: str, *, nc: bool = False) -> str:
    return _ansi("90", t, nc=nc)


# ===========================================================================
# C. 출력 렌더러
# ===========================================================================


class MarkdownRenderer:
    """LLM 텍스트 Markdown → 터미널 풍부한 텍스트 (bold/코드블록/목록)."""

    def __init__(self, *, no_color: bool = False, width: int = 0) -> None:
        self.nc = no_color
        self.width = width or shutil.get_terminal_size().columns

    def render(self, text: str) -> str:
        """마크다운 텍스트를 ANSI 이스케이프로 변환."""
        lines = text.split("\n")
        out: list[str] = []
        in_code = False
        code_lang = ""
        code_lines: list[str] = []

        for line in lines:
            # 코드 블록 진입/종료
            if line.startswith("```"):
                if not in_code:
                    in_code = True
                    code_lang = line[3:].strip()
                    code_lines = []
                else:
                    # 코드 블록 렌더링
                    rendered = CodeBlockRenderer(no_color=self.nc).render(
                        "\n".join(code_lines), lang=code_lang
                    )
                    out.append(rendered)
                    in_code = False
                    code_lines = []
                continue

            if in_code:
                code_lines.append(line)
                continue

            # 제목
            if line.startswith("### "):
                out.append(bold(line[4:], nc=self.nc))  # pragma: no cover
            elif line.startswith("## "):
                out.append(bold(cyan(line[3:], nc=self.nc), nc=self.nc))  # pragma: no cover
            elif line.startswith("# "):
                out.append(bold(cyan(line[2:], nc=self.nc), nc=self.nc))
            # 목록
            elif line.startswith("- ") or line.startswith("* "):
                out.append(f"  • {line[2:]}")
            elif line.startswith("  - ") or line.startswith("  * "):
                out.append(f"    · {line[4:]}")  # pragma: no cover
            # 번호 목록
            elif len(line) > 2 and line[0].isdigit() and line[1] in ".)" and line[2] == " ":
                out.append(f"  {line}")  # pragma: no cover
            # 인용
            elif line.startswith("> "):
                out.append(gray(f"  │ {line[2:]}", nc=self.nc))
            # bold **text**
            else:
                rendered = self._inline(line)
                out.append(rendered)

        # 닫히지 않은 코드 블록
        if in_code and code_lines:
            out.append(
                CodeBlockRenderer(no_color=self.nc).render(  # pragma: no cover
                    "\n".join(code_lines), lang=code_lang
                )
            )

        return "\n".join(out)

    def _inline(self, text: str) -> str:
        """인라인 마크다운 (bold/italic/code) 처리."""
        import re

        # `code`
        text = re.sub(
            r"`([^`]+)`",
            lambda m: _ansi("7", m.group(1), nc=self.nc) if not self.nc else f"`{m.group(1)}`",
            text,
        )
        # **bold**
        text = re.sub(r"\*\*([^*]+)\*\*", lambda m: bold(m.group(1), nc=self.nc), text)
        # *italic*
        text = re.sub(r"\*([^*]+)\*", lambda m: _ansi("3", m.group(1), nc=self.nc), text)
        return text

    def print(self, text: str) -> None:
        print(self.render(text))  # pragma: no cover


class CodeBlockRenderer:
    """코드 블록 구문 하이라이팅 (pygments fallback)."""

    # 최소 ANSI 키워드 색상 (pygments 없을 때)
    _KEYWORDS = {
        "python": [
            "def",
            "class",
            "import",
            "from",
            "return",
            "if",
            "else",
            "elif",
            "for",
            "while",
            "try",
            "except",
            "with",
            "as",
            "async",
            "await",
            "True",
            "False",
            "None",
        ],
        "javascript": [
            "function",
            "const",
            "let",
            "var",
            "return",
            "if",
            "else",
            "for",
            "while",
            "class",
            "import",
            "export",
        ],
    }

    def __init__(self, *, no_color: bool = False) -> None:
        self.nc = no_color

    def render(self, code: str, *, lang: str = "") -> str:
        """코드를 하이라이팅된 문자열로 반환."""
        if self.nc:
            header = f"  [{lang}]" if lang else ""
            lines = ["  " + ln for ln in code.splitlines()]
            return "\n".join([header] + lines) if header else "\n".join(lines)

        # pygments 시도
        try:
            from pygments import highlight
            from pygments.formatters import Terminal256Formatter
            from pygments.lexers import get_lexer_by_name, guess_lexer

            try:
                lexer = get_lexer_by_name(lang) if lang else guess_lexer(code)
            except Exception:  # pragma: no cover
                from pygments.lexers import TextLexer  # pragma: no cover

                lexer = TextLexer()  # pragma: no cover
            highlighted = highlight(code, lexer, Terminal256Formatter(style="monokai"))
            header = gray(f"  ╭─ {lang} ", nc=False) if lang else gray("  ╭─", nc=False)
            footer = gray("  ╰─", nc=False)
            indented = "\n".join("  │ " + ln for ln in highlighted.rstrip().splitlines())
            return f"{header}\n{indented}\n{footer}"
        except ImportError:  # pragma: no cover
            pass  # pragma: no cover

        # 최소 폴백
        kws = self._KEYWORDS.get(lang.lower(), [])  # pragma: no cover
        lines = []  # pragma: no cover
        for line in code.splitlines():  # pragma: no cover
            for kw in kws:  # pragma: no cover
                line = line.replace(kw, _ansi("35", kw))  # pragma: no cover
            lines.append("  │ " + line)  # pragma: no cover
        header = (
            gray(f"  ╭─ {lang}", nc=False) if lang else gray("  ╭─", nc=False)
        )  # pragma: no cover
        footer = gray("  ╰─", nc=False)  # pragma: no cover
        return header + "\n" + "\n".join(lines) + "\n" + footer  # pragma: no cover


class ToolCallRenderer:
    """도구 호출 표시: ⚙ bash_exec "ls -la"."""

    def __init__(self, *, no_color: bool = False) -> None:
        self.nc = no_color

    def render(self, tool_name: str, tool_input: dict) -> str:
        icon = "⚙"
        name_str = cyan(tool_name, nc=self.nc)
        # 핵심 인수만 한 줄에
        key_arg = ""
        for key in ("command", "path", "pattern", "query", "url", "task"):
            if key in tool_input:
                val = str(tool_input[key])[:80]
                key_arg = gray(f' "{val}"', nc=self.nc)
                break
        return f"\n{icon} {name_str}{key_arg}"

    def print(self, tool_name: str, tool_input: dict) -> None:
        print(self.render(tool_name, tool_input))  # pragma: no cover


class ToolResultRenderer:
    """도구 결과 표시: 잘라냄/접기 긴 출력."""

    MAX_LINES = 20
    MAX_CHARS = 1200

    def __init__(self, *, no_color: bool = False) -> None:
        self.nc = no_color

    def render(self, result: dict | str, *, tool_name: str = "") -> str:
        if isinstance(result, dict):
            if "error" in result:
                return red(f"  ✗ {result['error']}", nc=self.nc)
            # 주요 필드 추출
            text = (
                result.get("content")
                or result.get("stdout")
                or result.get("diff")
                or result.get("matches")
                or result.get("summary")
                or str(result)
            )
        else:
            text = str(result)

        if isinstance(text, list):
            text = "\n".join(str(x) for x in text[:20])  # pragma: no cover

        text = str(text)
        lines = text.splitlines()

        truncated = False
        if len(lines) > self.MAX_LINES:
            lines = lines[: self.MAX_LINES]
            truncated = True
        if len(text) > self.MAX_CHARS:
            text = text[: self.MAX_CHARS]  # pragma: no cover
            truncated = True  # pragma: no cover
            lines = text.splitlines()  # pragma: no cover

        out = []
        for line in lines:
            out.append(gray("  │ ", nc=self.nc) + line)
        if truncated:
            out.append(dim("  │ … (output truncated)", nc=self.nc))

        return "\n".join(out)

    def print(self, result: dict | str, *, tool_name: str = "") -> None:
        print(self.render(result, tool_name=tool_name))  # pragma: no cover


class ThinkingBlock:
    """💭 thinking 접기 표시 (기본 접힘, t키로 전개)."""

    def __init__(self, *, no_color: bool = False, collapsed: bool = True) -> None:
        self.nc = no_color
        self.collapsed = collapsed
        self._full_text = ""

    def render(self, thinking: str) -> str:
        self._full_text = thinking
        if not thinking:
            return ""
        if self.collapsed:
            preview = thinking[:100].replace("\n", " ")
            return gray(f"  💭 {preview}…  [thinking]", nc=self.nc)
        else:
            lines = thinking.splitlines()
            header = gray("  💭 Thinking:", nc=self.nc)
            body = "\n".join(gray(f"    {ln}", nc=self.nc) for ln in lines)
            return f"{header}\n{body}"

    def toggle(self) -> str:
        self.collapsed = not self.collapsed
        return self.render(self._full_text)

    def print(self, thinking: str) -> None:
        rendered = self.render(thinking)
        if rendered:
            print(rendered)


class StreamPrinter:
    """스트리밍 token 점진적 출력, Ctrl+C 인터럽트 지원."""

    def __init__(self, *, no_color: bool = False, file=None) -> None:
        self.nc = no_color
        self._file = file or sys.stdout
        self._interrupted = False

    def set_interrupted(self) -> None:
        self._interrupted = True

    async def print_stream(self, stream) -> tuple[str, bool]:
        """스트림 소비, (full_text, was_interrupted) 반환."""
        full_text = ""
        thinking_buf = ""
        self._interrupted = False

        thinking_renderer = ThinkingBlock(no_color=self.nc)
        showed_thinking = False

        try:
            async for delta in stream:
                if self._interrupted:
                    print(dim("\n  [interrupted]", nc=self.nc), file=self._file)  # pragma: no cover
                    break  # pragma: no cover

                dtype = (
                    getattr(delta, "type", delta.get("type", ""))
                    if isinstance(delta, dict)
                    else getattr(delta, "type", "")
                )

                if dtype == "thinking":
                    t = (
                        getattr(delta, "text", delta.get("thinking", ""))
                        if isinstance(delta, dict)
                        else getattr(delta, "text", "")
                    )
                    thinking_buf += t
                    if not showed_thinking:
                        thinking_renderer.print(thinking_buf)
                        showed_thinking = True

                elif dtype == "text":
                    t = (
                        getattr(delta, "text", delta.get("text", ""))
                        if isinstance(delta, dict)
                        else getattr(delta, "text", "")
                    )
                    print(t, end="", flush=True, file=self._file)
                    full_text += t

                elif dtype == "stop":
                    print("", file=self._file)  # 최종 개행

        except (asyncio.CancelledError, KeyboardInterrupt):  # pragma: no cover
            self._interrupted = True  # pragma: no cover
            print(dim("\n  [interrupted]", nc=self.nc), file=self._file)  # pragma: no cover

        return full_text, self._interrupted


# ===========================================================================
# D. 상태표시줄 & 레이아웃
# ===========================================================================


class LayoutManager:
    """터미널 너비 자동 조정 (좁은 터미널 폴백 일반 텍스트)."""

    NARROW_THRESHOLD = 60

    @staticmethod
    def width() -> int:
        return shutil.get_terminal_size(fallback=(80, 24)).columns

    @staticmethod
    def height() -> int:
        return shutil.get_terminal_size(fallback=(80, 24)).lines

    @staticmethod
    def is_narrow() -> bool:
        return LayoutManager.width() < LayoutManager.NARROW_THRESHOLD

    @staticmethod
    def wrap(text: str, indent: int = 2) -> str:
        w = LayoutManager.width() - indent
        return textwrap.fill(
            text, width=max(w, 40), initial_indent=" " * indent, subsequent_indent=" " * indent
        )


class SpinnerAnimation:
    """도구 실행 중 스피너 (⠋⠙⠹⠸⠼⠴⠦⠧⠇⠏)."""

    FRAMES = "⠋⠙⠹⠸⠼⠴⠦⠧⠇⠏"

    def __init__(self, *, label: str = "thinking", no_color: bool = False, file=None) -> None:
        self.label = label
        self.nc = no_color
        self._file = file or sys.stderr
        self._stop = threading.Event()
        self._thread: threading.Thread | None = None

    def start(self, label: str | None = None) -> None:
        if label:
            self.label = label
        self._stop.clear()
        self._thread = threading.Thread(target=self._spin, daemon=True)
        self._thread.start()

    def stop(self) -> None:
        self._stop.set()
        if self._thread:
            self._thread.join(timeout=0.5)
        # 스피너 줄 지우기
        print(f"\r{' ' * (len(self.label) + 4)}\r", end="", file=self._file, flush=True)

    def _spin(self) -> None:
        for frame in itertools.cycle(self.FRAMES):
            if self._stop.is_set():
                break
            label_str = gray(self.label, nc=self.nc)
            print(f"\r  {frame} {label_str}", end="", file=self._file, flush=True)
            time.sleep(0.08)

    @contextmanager
    def context(self, label: str = "") -> Iterator[None]:
        self.start(label or self.label)
        try:
            yield
        finally:
            self.stop()


class DividerLine:
    """사용자/어시스턴트 메시지 구분선 (─────)."""

    @staticmethod
    def print(*, no_color: bool = False, char: str = "─") -> None:
        w = LayoutManager.width()
        line = char * min(w, 72)
        print(gray(line, nc=no_color))

    @staticmethod
    def user_prefix(*, no_color: bool = False) -> str:
        return bold(green("You", nc=no_color), nc=no_color) + "  "

    @staticmethod
    def assistant_prefix(*, no_color: bool = False) -> str:
        return bold(cyan("veya", nc=no_color), nc=no_color) + "  "


class StatusBar:
    """
    하단 상태표시줄: 모드 | 모델 | cost | tokens | session ID.
    stderr에 출력해서 stdout 파이프에 영향 없음.
    """

    def __init__(self, *, no_color: bool = False, file=None) -> None:
        self.nc = no_color
        self._file = file or sys.stderr
        self.mode = "build"
        self.model = "claude-sonnet-4-6"
        self.cost_usd = 0.0
        self.in_tokens = 0
        self.out_tokens = 0
        self.session_id = ""

    def update(
        self,
        *,
        mode: str | None = None,
        model: str | None = None,
        cost: float = 0.0,
        in_tok: int = 0,
        out_tok: int = 0,
        session_id: str | None = None,
    ) -> None:
        if mode:
            self.mode = mode
        if model:
            self.model = model
        self.cost_usd += cost
        self.in_tokens += in_tok
        self.out_tokens += out_tok
        if session_id:
            self.session_id = session_id

    def render(self) -> str:
        mode_str = _ansi("36" if not self.nc else "", self.mode.upper(), nc=self.nc)
        cost_str = f"${self.cost_usd:.4f}"
        tok_str = f"{self.in_tokens + self.out_tokens:,}tok"
        model_short = self.model.replace("claude-", "").replace("-latest", "")
        sess = f" · {self.session_id[:8]}" if self.session_id else ""

        if LayoutManager.is_narrow():
            return f" {self.mode.upper()} · {cost_str}"  # pragma: no cover

        parts = [
            gray(f"  {mode_str}", nc=self.nc),
            gray(f"│ {model_short}", nc=self.nc),
            gray(f"│ {cost_str}", nc=self.nc),
            gray(f"│ {tok_str}", nc=self.nc),
            gray(f"{sess}", nc=self.nc),
        ]
        return "  ".join(parts)

    def print(self) -> None:
        line = self.render()
        print(f"\r{line}", end="", file=self._file, flush=True)

    def clear(self) -> None:
        w = LayoutManager.width()
        print(f"\r{' ' * w}\r", end="", file=self._file, flush=True)


# ===========================================================================
# E. 인터랙션 컨트롤
# ===========================================================================


class InterruptHandler:
    """
    Ctrl+C 우아한 인터럽트: 스트리밍 출력 중단, 프로세스 종료 안 함.
    두 번 Ctrl+C → 실제 종료.
    """

    def __init__(self) -> None:
        self._count = 0
        self._last_time = 0.0
        self._callbacks: list = []
        self._original_handler: Any = None

    def register_callback(self, fn) -> None:
        self._callbacks.append(fn)

    def install(self) -> None:
        self._original_handler = signal.getsignal(signal.SIGINT)
        signal.signal(signal.SIGINT, self._handle)

    def uninstall(self) -> None:
        if self._original_handler:
            signal.signal(signal.SIGINT, self._original_handler)

    def _handle(self, sig, frame) -> None:
        now = time.time()
        if now - self._last_time < 2.0:
            self._count += 1
        else:
            self._count = 1
        self._last_time = now

        for cb in self._callbacks:
            try:
                cb()
            except Exception:  # pragma: no cover
                pass  # pragma: no cover

        if self._count >= 2:
            print("\n  Ctrl+C pressed twice — exiting.", file=sys.stderr)
            sys.exit(0)
        else:
            print("\n  [interrupted] Press Ctrl+C again to exit.", file=sys.stderr)


class ExitHandler:
    """Ctrl+D / /exit 확인 종료, 세션 저장."""

    def __init__(self, *, session_manager=None) -> None:
        self._mgr = session_manager

    def handle(self, session=None) -> bool:
        """True → 종료해야 함."""
        try:
            choice = input("\n  Exit veya? [y/N] ").strip().lower()
        except (EOFError, KeyboardInterrupt):
            choice = "y"
        if choice in ("y", "yes"):
            if self._mgr and session:
                self._mgr.save(session)
            print("  Goodbye!", file=sys.stderr)
            return True
        return False


class YesNoPrompt:
    """permission gate의 y/n/always 터미널 인터랙션."""

    def __init__(self, *, no_color: bool = False, auto: str | None = None) -> None:
        self.nc = no_color
        self._auto = auto  # "allow" | "deny" | None

    async def ask(self, tool_name: str, tool_input: dict) -> str:
        """'allow' | 'always' | 'deny' | 'deny_always' 반환."""
        if self._auto == "allow":
            return "allow"
        if self._auto == "deny":
            return "deny"

        import json

        print()
        print(yellow(f"  ⚠  {tool_name}", nc=self.nc))
        preview = json.dumps(tool_input, ensure_ascii=False)[:120]
        print(gray(f"     {preview}", nc=self.nc))
        print()

        try:
            resp = input("  Allow? [y/n/always/no-always] ").strip().lower()
        except (EOFError, KeyboardInterrupt):
            resp = "n"

        return {
            "y": "allow",
            "yes": "allow",
            "a": "always",
            "always": "always",
            "n": "deny",
            "no": "deny",
            "d": "deny_always",
            "no-always": "deny_always",
        }.get(resp, "deny")


class PagerView:
    """긴 출력 페이지 (q 종료, space 다음 페이지)."""

    def __init__(self, *, lines_per_page: int = 0) -> None:
        self._lpp = lines_per_page or LayoutManager.height() - 3

    def show(self, text: str) -> None:
        lines = text.splitlines()
        if len(lines) <= self._lpp:
            print(text)
            return
        i = 0
        while i < len(lines):
            chunk = "\n".join(lines[i : i + self._lpp])
            print(chunk)
            i += self._lpp
            if i < len(lines):
                try:
                    key = input(gray(f"  -- [{i}/{len(lines)} lines] space=더보기 q=종료 --"))
                    if key.strip().lower() in ("q", "quit"):
                        break
                except (EOFError, KeyboardInterrupt):  # pragma: no cover
                    break  # pragma: no cover


class CopyHint:
    """코드 블록 옆 [copy] 힌트 (터미널 OSC 52 지원 시 클립보드 복사)."""

    @staticmethod
    def copy_to_clipboard(text: str) -> bool:
        """터미널 OSC 52를 통해 클립보드에 복사. 성공 여부 반환."""
        import base64

        try:
            encoded = base64.b64encode(text.encode()).decode()
            sys.stdout.write(f"\033]52;c;{encoded}\a")
            sys.stdout.flush()
            return True
        except Exception:  # pragma: no cover
            return False  # pragma: no cover

    @staticmethod
    def hint(*, no_color: bool = False) -> str:
        return gray("  [copy]", nc=no_color)

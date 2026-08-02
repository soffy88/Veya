"""
hicode_tui.input — A. CLI 入口 & B. Prompt 输入框
===================================================
A: hicode_cli / ArgParser / EnvLoader / StartupBanner
B: PromptInput / HistoryManager / AtMentionCompleter
   SlashCompleter / MultilineInput
"""
from __future__ import annotations

import os
import readline
import sys
from dataclasses import dataclass
from pathlib import Path
from typing import Any


# ---------------------------------------------------------------------------
# A. CLI 入口
# ---------------------------------------------------------------------------

VERSION = "0.1.0"

BANNER_COLORS = {
    "green":  "\033[32m",
    "cyan":   "\033[36m",
    "yellow": "\033[33m",
    "bold":   "\033[1m",
    "dim":    "\033[2m",
    "reset":  "\033[0m",
}


def _c(color: str, text: str, *, no_color: bool = False) -> str:
    if no_color:
        return text
    return f"{BANNER_COLORS.get(color, '')}{text}{BANNER_COLORS['reset']}"


@dataclass
class CliArgs:
    """파싱된 CLI 인수."""
    task: str = ""
    model: str = "claude-sonnet-4-6"
    mode: str = "build"
    cwd: str = ""
    session_id: str = ""
    no_color: bool = False
    print_mode: bool = False        # -p / --print
    continue_session: bool = False  # -c / --continue
    verbose: bool = False
    version: bool = False


class ArgParser:
    """
    경량 CLI 인수 파서 (Click 불필요, 직접 sys.argv 파싱).
    
    사용법:
      hicode [task]
      hicode -p 'fix the tests'
      hicode --model claude-opus-4-6 --mode plan
      hicode --session sess_abc123 --continue
    """

    HELP = """\
hicode — AI 编码 agent  v{version}

使用方式:
  hicode [TASK]              交互式 REPL（task 可选，作为首条消息）
  hicode -p TASK             非交互模式（stdout=结果，适合管道）
  hicode -c                  继续上一个会话

选项:
  --model MODEL              LLM 模型（默认 claude-sonnet-4-6）
  --mode build|plan          运行模式（默认 build）
  --cwd PATH                 工作目录（默认当前目录）
  --session SESSION_ID       指定会话 ID
  -p, --print TASK           非交互单次执行
  -c, --continue             继续上次会话
  --no-color                 禁用 ANSI 颜色
  -v, --verbose              详细日志
  --version                  显示版本
  -h, --help                 显示帮助
""".format(version=VERSION)

    @classmethod
    def parse(cls, argv: list[str] | None = None) -> CliArgs:
        args = argv if argv is not None else sys.argv[1:]
        result = CliArgs()
        i = 0
        positional = []

        while i < len(args):
            a = args[i]
            if a in ("-h", "--help"):
                print(cls.HELP)  # pragma: no cover
                sys.exit(0)  # pragma: no cover
            elif a == "--version":
                print(f"hicode {VERSION}")  # pragma: no cover
                sys.exit(0)  # pragma: no cover
            elif a in ("-p", "--print") and i + 1 < len(args):
                result.print_mode = True
                result.task = args[i + 1]
                i += 2
                continue
            elif a in ("-p", "--print"):
                result.print_mode = True  # pragma: no cover
            elif a in ("-c", "--continue"):
                result.continue_session = True
            elif a in ("-v", "--verbose"):
                result.verbose = True
            elif a == "--no-color":
                result.no_color = True
            elif a == "--model" and i + 1 < len(args):
                result.model = args[i + 1]
                i += 2
                continue
            elif a == "--mode" and i + 1 < len(args):
                result.mode = args[i + 1]
                i += 2
                continue
            elif a == "--cwd" and i + 1 < len(args):
                result.cwd = args[i + 1]
                i += 2
                continue
            elif a == "--session" and i + 1 < len(args):
                result.session_id = args[i + 1]
                i += 2
                continue
            elif not a.startswith("-"):
                positional.append(a)
            i += 1

        if positional and not result.task:
            result.task = " ".join(positional)
        if not result.cwd:
            result.cwd = os.getcwd()
        return result


class EnvLoader:
    """시작 시 .env + CredentialStore 로드, API 키 검증."""

    @staticmethod
    def load(*, verbose: bool = False) -> dict[str, str]:
        """환경 변수 로드. 반환값: {"key": "value"} 딕셔너리."""
        loaded: dict[str, str] = {}

        # ~/.hicode/.env
        env_file = Path.home() / ".hicode" / ".env"
        if env_file.exists():
            for line in env_file.read_text().splitlines():  # pragma: no cover
                line = line.strip()  # pragma: no cover
                if line and not line.startswith("#") and "=" in line:  # pragma: no cover
                    k, _, v = line.partition("=")  # pragma: no cover
                    k = k.strip()  # pragma: no cover
                    if k and k not in os.environ:  # pragma: no cover
                        os.environ[k] = v.strip()  # pragma: no cover
                        loaded[k] = v.strip()  # pragma: no cover
                        if verbose:  # pragma: no cover
                            print(f"  loaded {k} from ~/.hicode/.env", file=sys.stderr)  # pragma: no cover

        return loaded

    @staticmethod
    def check_api_key(provider: str = "anthropic") -> bool:
        """检查对应 provider 的 API key 是否已设置。"""
        key_map = {
            "anthropic": "ANTHROPIC_API_KEY",
            "deepseek":  "DEEPSEEK_API_KEY",
        }
        key_name = key_map.get(provider, f"{provider.upper()}_API_KEY")
        return bool(os.environ.get(key_name))

    @staticmethod
    def detect_provider(model: str) -> str:
        """根据模型名推断 provider。"""
        if model.startswith("deepseek"):
            return "deepseek"
        if model.startswith("claude"):
            return "anthropic"
        return "anthropic"  # pragma: no cover

    @staticmethod
    def get_caller(model: str = "claude-sonnet-4-6") -> Any:
        """根据模型名自动选择 provider，构造 LLMCaller。"""
        try:
            from hicode_tui.providers import get_caller  # pragma: no cover
            return get_caller(model)  # pragma: no cover
        except Exception:
            return None

class StartupBanner:
    """버전 + 모델 + 모드 배너 출력."""

    @staticmethod
    def print(args: CliArgs) -> None:
        nc = args.no_color
        if args.print_mode:
            return  # -p 모드에서는 배너 없음

        mode_color = "cyan" if args.mode == "build" else "yellow"
        mode_icon = "🔨" if args.mode == "build" else "📋"

        print()
        print(_c("bold", f"  hicode {VERSION}", no_color=nc) +
              "  " + _c("dim", "AI coding agent", no_color=nc))
        print()
        print(f"  model  {_c('green', args.model, no_color=nc)}")
        print(f"  mode   {mode_icon} {_c(mode_color, args.mode.upper(), no_color=nc)}")
        print(f"  cwd    {_c('dim', args.cwd, no_color=nc)}")
        print()
        print(_c("dim", "  /help for commands · Ctrl+C to interrupt · Ctrl+D to exit",
                  no_color=nc))
        print()


# ---------------------------------------------------------------------------
# B. Prompt 输入框
# ---------------------------------------------------------------------------

SLASH_COMMANDS = [
    "/init", "/plan", "/build", "/undo", "/redo", "/compact",
    "/review", "/tests", "/checkpoint", "/rewind", "/agents",
    "/plugin", "/hooks", "/sessions", "/help", "/exit",
]


class HistoryManager:
    """~/.hicode/history 영속 저장, ↑↓ 탐색."""

    HISTORY_FILE = Path.home() / ".hicode" / "history"
    MAX_ENTRIES = 2000

    @classmethod
    def setup(cls) -> None:
        cls.HISTORY_FILE.parent.mkdir(parents=True, exist_ok=True)
        try:
            readline.read_history_file(str(cls.HISTORY_FILE))
        except FileNotFoundError:
            pass
        readline.set_history_length(cls.MAX_ENTRIES)

    @classmethod
    def save(cls) -> None:
        try:
            readline.write_history_file(str(cls.HISTORY_FILE))
        except Exception:  # pragma: no cover
            pass  # pragma: no cover

    @classmethod
    def add(cls, entry: str) -> None:
        if entry.strip():  # pragma: no cover
            readline.add_history(entry)  # pragma: no cover


class AtMentionCompleter:
    """@ 트리거 파일/심볼 보완 (Tab 전개)."""

    def __init__(self, cwd: str = ".") -> None:
        self.cwd = Path(cwd)

    def _candidates(self, prefix: str) -> list[str]:
        """@ 뒤 prefix에 매칭하는 파일 경로 목록."""
        root = self.cwd
        candidates: list[str] = []
        try:
            for p in root.rglob("*"):
                if p.is_file() and len(candidates) < 50:
                    rel = str(p.relative_to(root))
                    if prefix.lower() in rel.lower():
                        candidates.append(rel)
        except Exception:  # pragma: no cover
            pass  # pragma: no cover
        return sorted(candidates)[:20]

    def complete(self, text: str, state: int) -> str | None:
        # readline complete 콜백 형태
        line = readline.get_line_buffer()
        at_idx = line.rfind("@")
        if at_idx == -1:
            return None
        after_at = line[at_idx + 1:]  # pragma: no cover
        candidates = self._candidates(after_at)  # pragma: no cover
        if state < len(candidates):  # pragma: no cover
            return candidates[state]  # pragma: no cover
        return None  # pragma: no cover


class SlashCompleter:
    """/ 트리거 커맨드 보완."""

    def __init__(self, commands: list[str] | None = None) -> None:
        self.commands = commands or SLASH_COMMANDS

    def complete(self, text: str, state: int) -> str | None:
        line = readline.get_line_buffer()
        if not line.startswith("/"):
            return None
        matches = [c for c in self.commands if c.startswith(line)]
        if state < len(matches):
            return matches[state]
        return None


class MultilineInput:
    r"""
    \ 결행 계속, Enter 제출, Esc 취소.
    빈 입력 + Enter → None 반환 (무시).
    """

    @staticmethod
    def read(prompt: str = "> ") -> str | None:
        """여러 줄 입력을 읽어 하나의 문자열로 반환. None이면 취소/종료."""
        lines = []
        first = True
        while True:
            try:
                cont_prompt = "... " if not first else prompt
                line = input(cont_prompt)
            except KeyboardInterrupt:
                print()
                return None   # Ctrl+C → 취소
            except EOFError:
                print()
                raise          # Ctrl+D → 호출자에서 처리

            first = False
            if line.endswith("\\"):
                lines.append(line[:-1])
                continue
            else:
                lines.append(line)
                break

        result = "\n".join(lines).strip()
        return result if result else None


class PromptInput:
    """
    readline 강화 입력박스 (통합 진입점).
    역할: HistoryManager + AtMentionCompleter + SlashCompleter + MultilineInput 통합.
    """

    def __init__(self, *, cwd: str = ".", no_color: bool = False) -> None:
        self.cwd = cwd  # pragma: no cover
        self.no_color = no_color  # pragma: no cover
        self._at_completer = AtMentionCompleter(cwd=cwd)  # pragma: no cover
        self._slash_completer = SlashCompleter()  # pragma: no cover
        HistoryManager.setup()  # pragma: no cover
        self._setup_readline()  # pragma: no cover

    def _setup_readline(self) -> None:
        """readline 보완 통합 등록."""
        def completer(text: str, state: int) -> str | None:  # pragma: no cover
            line = readline.get_line_buffer()  # pragma: no cover
            if line.startswith("/"):  # pragma: no cover
                return self._slash_completer.complete(text, state)  # pragma: no cover
            if "@" in line:  # pragma: no cover
                return self._at_completer.complete(text, state)  # pragma: no cover
            return None  # pragma: no cover

        readline.set_completer(completer)  # pragma: no cover
        readline.set_completer_delims(" \t\n")  # pragma: no cover
        try:  # pragma: no cover
            readline.parse_and_bind("tab: complete")  # pragma: no cover
        except Exception:  # pragma: no cover
            pass  # pragma: no cover

    def prompt_str(self) -> str:
        if self.no_color:  # pragma: no cover
            return "> "  # pragma: no cover
        return "\033[32m❯\033[0m "  # pragma: no cover

    def read(self) -> str | None:
        """한 번의 사용자 입력을 읽음. None=Ctrl+C 취소."""
        try:  # pragma: no cover
            text = MultilineInput.read(self.prompt_str())  # pragma: no cover
            if text:  # pragma: no cover
                HistoryManager.add(text)  # pragma: no cover
            return text  # pragma: no cover
        except EOFError:  # pragma: no cover
            return None  # Ctrl+D → 종료 신호  # pragma: no cover

    def close(self) -> None:
        HistoryManager.save()  # pragma: no cover

"""hicode_tui — hicode 터미널 UI."""

from .input import (
    VERSION,
    ArgParser,
    AtMentionCompleter,
    CliArgs,
    EnvLoader,
    HistoryManager,
    MultilineInput,
    PromptInput,
    SlashCompleter,
    StartupBanner,
)
from .providers import (
    detect_provider,
    make_deepseek_caller,
    make_openai_compat_caller,
)
from .providers import (
    get_caller as get_provider_caller,
)
from .render import (
    CodeBlockRenderer,
    CopyHint,
    DividerLine,
    ExitHandler,
    InterruptHandler,
    LayoutManager,
    MarkdownRenderer,
    PagerView,
    SpinnerAnimation,
    StatusBar,
    StreamPrinter,
    ThinkingBlock,
    ToolCallRenderer,
    ToolResultRenderer,
    YesNoPrompt,
)
from .repl import (
    AgentLoopAdapter,
    HicodeREPL,
    LoopOrchestrator,
    PipeFriendly,
    PrintMode,
    SessionContext,
    build_session_context,
    main,
)

__all__ = [
    # input
    "ArgParser",
    "CliArgs",
    "EnvLoader",
    "HistoryManager",
    "AtMentionCompleter",
    "SlashCompleter",
    "MultilineInput",
    "PromptInput",
    "StartupBanner",
    "VERSION",
    # render
    "MarkdownRenderer",
    "CodeBlockRenderer",
    "ToolCallRenderer",
    "ToolResultRenderer",
    "ThinkingBlock",
    "StreamPrinter",
    "StatusBar",
    "LayoutManager",
    "SpinnerAnimation",
    "DividerLine",
    "InterruptHandler",
    "ExitHandler",
    "YesNoPrompt",
    "PagerView",
    "CopyHint",
    # providers
    "make_deepseek_caller",
    "make_openai_compat_caller",
    "detect_provider",
    "get_provider_caller",
    # repl
    "SessionContext",
    "build_session_context",
    "AgentLoopAdapter",
    "LoopOrchestrator",
    "HicodeREPL",
    "PrintMode",
    "PipeFriendly",
    "main",
]

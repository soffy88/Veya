"""
智能工具集成模块 - P1 核心能力
功能：智能 Git、终端命令、输出解析、工具建议
"""

from __future__ import annotations

import asyncio
import json
import logging
import os
import re
import shlex
import time
from dataclasses import dataclass, field
from enum import Enum, StrEnum
from typing import Any


class ToolType(StrEnum):
    """工具类型枚举"""

    GIT = "git"
    TERMINAL = "terminal"
    FILESYSTEM = "filesystem"
    HTTP = "http"
    CUSTOM = "custom"


class ToolStatus(StrEnum):
    """工具执行状态"""

    PENDING = "pending"
    RUNNING = "running"
    SUCCESS = "success"
    FAILED = "failed"
    CANCELLED = "cancelled"


class ToolPriority(int, Enum):
    """工具优先级"""

    LOW = 1
    MEDIUM = 2
    HIGH = 3


@dataclass
class ToolResult:
    """工具执行结果"""

    command: str
    status: ToolStatus
    output: str = ""
    error: str = ""
    duration: float = 0.0
    exit_code: int = 0
    parsed_output: dict[str, Any] | None = None
    suggestions: list[str] = field(default_factory=list)
    timestamp: float = field(default_factory=time.time)
    context: dict[str, Any] = field(default_factory=dict)


@dataclass
class ToolMetadata:
    """工具元数据"""

    name: str
    type: ToolType
    description: str
    parameters: dict[str, dict[str, Any]] = field(default_factory=dict)
    examples: list[str] = field(default_factory=list)
    priority: ToolPriority = ToolPriority.MEDIUM
    requires_context: bool = True
    safe_to_retry: bool = True

    def validate_parameters(self, args: dict[str, Any]) -> tuple[bool, str]:
        """验证参数"""
        for param, spec in self.parameters.items():
            if spec.get("required", False) and param not in args:
                return False, f"Missing required parameter: {param}"

            value = args.get(param)
            if value is not None and "type" in spec:
                expected_type = spec["type"]
                if expected_type == "int":
                    try:
                        int(value)
                    except ValueError:
                        return False, f"Parameter {param} must be integer"
                elif expected_type == "float":
                    try:
                        float(value)
                    except ValueError:
                        return False, f"Parameter {param} must be float"
                elif expected_type == "bool":
                    if not isinstance(value, bool):
                        return False, f"Parameter {param} must be boolean"
                elif expected_type == "str":
                    if not isinstance(value, str):
                        return False, f"Parameter {param} must be string"
                elif expected_type == "list" and not isinstance(value, list):
                    return False, f"Parameter {param} must be list"
        return True, ""


class SmartTool:
    """
    智能工具基础类

    功能：
    1. 命令执行与输出解析
    2. 上下文感知
    3. 智能建议
    4. 安全执行
    5. 重试机制
    """

    def __init__(self, metadata: ToolMetadata):
        self.metadata = metadata
        self.logger = logging.getLogger(f"tool.{metadata.name}")
        self.context: dict[str, Any] = {}
        self.history: list[ToolResult] = []
        self.max_retries = 2
        self.last_execution_time = 0

    async def execute(self, **kwargs) -> ToolResult:
        """执行工具"""
        start_time = time.time()

        # 验证参数
        valid, error = self.metadata.validate_parameters(kwargs)
        if not valid:
            return ToolResult(
                command=str(kwargs),
                status=ToolStatus.FAILED,
                error=error,
                duration=time.time() - start_time,
            )

        # 设置上下文
        self.context = kwargs

        # 执行前钩子
        pre_result = await self.pre_execute(**kwargs)
        if pre_result and pre_result.status != ToolStatus.SUCCESS:
            return pre_result

        # 执行命令
        try:
            result = await self._run_command(**kwargs)

            # 执行后处理
            if result.status == ToolStatus.SUCCESS:
                post_result = await self.post_execute(result, **kwargs)
                return post_result
            return result
        except Exception as e:
            return ToolResult(
                command=str(kwargs),
                status=ToolStatus.FAILED,
                error=str(e),
                duration=time.time() - start_time,
            )

    async def _run_command(self, **kwargs) -> ToolResult:
        """实际运行命令（子类实现）"""
        raise NotImplementedError

    async def pre_execute(self, **kwargs) -> ToolResult | None:
        """执行前钩子"""
        return None

    async def post_execute(self, result: ToolResult, **kwargs) -> ToolResult:
        """执行后处理"""
        # 解析输出
        result.parsed_output = self.parse_output(result.output)

        # 生成建议
        result.suggestions = self.generate_suggestions(result)

        # 记录历史
        self.history.append(result)

        return result

    def parse_output(self, output: str) -> dict[str, Any] | None:
        """解析输出（子类实现）"""
        return None

    def generate_suggestions(self, result: ToolResult) -> list[str]:
        """生成后续建议"""
        return []

    def get_history(self) -> list[ToolResult]:
        """获取执行历史"""
        return self.history[-10:]  # 只返回最近10条

    def get_stats(self) -> dict[str, Any]:
        """获取工具统计信息"""
        success_count = sum(1 for r in self.history if r.status == ToolStatus.SUCCESS)
        return {
            "name": self.metadata.name,
            "total_executions": len(self.history),
            "success_rate": success_count / len(self.history) if self.history else 0,
            "avg_duration": sum(r.duration for r in self.history) / len(self.history)
            if self.history
            else 0,
            "last_execution": self.last_execution_time,
        }


# Git 工具实现


class GitTool(SmartTool):
    """
    智能 Git 工具

    功能：
    1. 智能命令建议
    2. 输出解析与可视化
    3. 安全命令执行
    4. 状态感知
    """

    def __init__(self):
        metadata = ToolMetadata(
            name="git",
            type=ToolType.GIT,
            description="Smart Git integration with command suggestions and output parsing",
            parameters={
                "command": {
                    "type": "str",
                    "required": True,
                    "description": "Git command to execute",
                },
                "path": {"type": "str", "default": ".", "description": "Repository path"},
            },
            examples=["git status", "git log -n 5", "git diff HEAD~1..HEAD", "git branch --all"],
            priority=ToolPriority.HIGH,
            requires_context=True,
        )
        super().__init__(metadata)

    async def _run_command(self, **kwargs) -> ToolResult:
        """执行 Git 命令"""
        command = kwargs["command"]
        repo_path = kwargs.get("path", ".")

        # 安全检查
        if not self._is_safe_command(command):
            return ToolResult(
                command=command,
                status=ToolStatus.FAILED,
                error="Potentially dangerous Git command detected",
                duration=0.0,
            )

        start_time = time.time()
        try:
            # 执行命令
            proc = await asyncio.create_subprocess_exec(
                "git",
                *shlex.split(command),
                cwd=repo_path,
                stdout=asyncio.subprocess.PIPE,
                stderr=asyncio.subprocess.PIPE,
            )
            stdout, stderr = await proc.communicate()

            # 处理输出
            output = stdout.decode().strip()
            error = stderr.decode().strip()

            return ToolResult(
                command=command,
                status=ToolStatus.SUCCESS if proc.returncode == 0 else ToolStatus.FAILED,
                output=output,
                error=error,
                duration=time.time() - start_time,
                exit_code=proc.returncode,
                context={"repo_path": repo_path},
            )
        except Exception as e:
            return ToolResult(
                command=command,
                status=ToolStatus.FAILED,
                error=str(e),
                duration=time.time() - start_time,
            )

    def _is_safe_command(self, command: str) -> bool:
        """检查 Git 命令是否安全"""
        dangerous_commands = ["reset --hard", "rebase -i", "push -f", "clean -fd", "filter-branch"]
        return not any(cmd in command for cmd in dangerous_commands)

    def parse_output(self, output: str) -> dict[str, Any] | None:
        """解析 Git 输出"""
        if not output:
            return None

        result = {"raw_output": output, "status": {}, "branches": [], "commits": [], "diffs": []}

        # 简单解析 - 实际应用中应更完善
        lines = output.split("\n")

        # Git status 解析
        if output.startswith("On branch"):
            result["status"]["current_branch"] = lines[0].split(" ")[2]

            # 解析修改
            changes = []
            for line in lines[1:]:
                if line.startswith("  "):
                    status, path = line.strip().split(maxsplit=1)
                    changes.append({"status": status, "path": path})
            result["status"]["changes"] = changes

        # Git log 解析
        elif any(line.startswith("commit ") for line in lines):
            current_commit = None
            for line in lines:
                if line.startswith("commit "):
                    if current_commit:
                        result["commits"].append(current_commit)
                    current_commit = {
                        "hash": line.split()[1],
                        "author": "",
                        "date": "",
                        "message": "",
                    }
                elif line.startswith("Author: "):
                    current_commit["author"] = line[8:].strip()
                elif line.startswith("Date: "):
                    current_commit["date"] = line[6:].strip()
                elif line.strip():
                    current_commit["message"] += line.strip() + " "
            if current_commit:
                result["commits"].append(current_commit)

        return result

    def generate_suggestions(self, result: ToolResult) -> list[str]:
        """生成 Git 相关建议"""
        suggestions = []
        parsed = result.parsed_output

        if parsed:
            # 状态建议
            if "status" in parsed and parsed["status"].get("changes"):
                changes = parsed["status"]["changes"]
                if any(c["status"] == "M" for c in changes):
                    suggestions.append("git add . - stage all changes")
                if any(c["status"] == "??" for c in changes):
                    suggestions.append("git add <file> - stage specific file")
                suggestions.append('git commit -m "<message>" - commit changes')

            # 分支建议
            if result.command.startswith("git branch"):
                suggestions.append("git checkout <branch> - switch to branch")
                suggestions.append("git merge <branch> - merge branch")

            # 提交历史建议
            if "commits" in parsed and len(parsed["commits"]) > 0:
                suggestions.append("git show <commit> - view commit details")
                suggestions.append("git diff <commit>^..<commit> - compare commits")

        return suggestions


# 终端工具实现


class TerminalTool(SmartTool):
    """
    智能终端工具

    功能：
    1. 安全命令执行
    2. 输出解析
    3. 交互式命令支持
    4. 环境感知
    """

    def __init__(self):
        metadata = ToolMetadata(
            name="terminal",
            type=ToolType.TERMINAL,
            description="Safe terminal command execution with output parsing",
            parameters={
                "command": {"type": "str", "required": True, "description": "Command to execute"},
                "path": {"type": "str", "default": ".", "description": "Working directory"},
            },
            examples=["ls -la", "npm install", "python -m pytest", "docker ps"],
            priority=ToolPriority.MEDIUM,
            requires_context=True,
        )
        super().__init__(metadata)

    async def _run_command(self, **kwargs) -> ToolResult:
        """执行终端命令"""
        command = kwargs["command"]
        cwd = kwargs.get("path", ".")

        # 安全检查
        if not self._is_safe_command(command):
            return ToolResult(
                command=command,
                status=ToolStatus.FAILED,
                error="Potentially dangerous command detected",
                duration=0.0,
            )

        start_time = time.time()
        try:
            # 执行命令
            proc = await asyncio.create_subprocess_shell(
                command, cwd=cwd, stdout=asyncio.subprocess.PIPE, stderr=asyncio.subprocess.PIPE
            )
            stdout, stderr = await proc.communicate()

            # 处理输出
            output = stdout.decode().strip()
            error = stderr.decode().strip()

            return ToolResult(
                command=command,
                status=ToolStatus.SUCCESS if proc.returncode == 0 else ToolStatus.FAILED,
                output=output,
                error=error,
                duration=time.time() - start_time,
                exit_code=proc.returncode,
                context={"cwd": cwd},
            )
        except Exception as e:
            return ToolResult(
                command=command,
                status=ToolStatus.FAILED,
                error=str(e),
                duration=time.time() - start_time,
            )

    def _is_safe_command(self, command: str) -> bool:
        """检查命令是否安全（单源委托 veya.sandbox.is_dangerous_command，§1.4）"""
        from veya.sandbox import is_dangerous_command

        return not is_dangerous_command(command)

    def parse_output(self, output: str) -> dict[str, Any] | None:
        """解析终端输出"""
        if not output:
            return None

        # 简单的解析示例 - 实际应根据命令类型定制
        result = {
            "raw_output": output,
            "lines": output.split("\n"),
            "line_count": len(output.split("\n")),
            "summary": {},
        }

        # 检测常见命令输出
        if "npm install" in self.context.get("command", ""):
            # 解析 npm 安装输出
            packages = []
            for line in output.split("\n"):
                if "added" in line and "package" in line:
                    parts = line.split()
                    if len(parts) > 3:
                        packages.append({"name": parts[2], "version": parts[3]})
            result["summary"] = {"packages_installed": len(packages), "packages": packages}

        elif "pytest" in self.context.get("command", ""):
            # 解析 pytest 输出
            match = re.search(r"(\d+) passed, (\d+) failed, (\d+) skipped", output)
            if match:
                result["summary"] = {
                    "passed": int(match.group(1)),
                    "failed": int(match.group(2)),
                    "skipped": int(match.group(3)),
                }

        return result

    def generate_suggestions(self, result: ToolResult) -> list[str]:
        """生成终端命令建议"""
        suggestions = []
        command = result.command
        parsed = result.parsed_output

        # 命令特定建议
        if "npm install" in command:
            suggestions.append("npm outdated - check for outdated packages")
            suggestions.append("npm list - depth=0 - view installed packages")
        elif "pytest" in command:
            if parsed and "summary" in parsed:
                if parsed["summary"].get("failed", 0) > 0:
                    suggestions.append("pytest -x - stop after first failure")
                    suggestions.append("pytest --lf - run only last failed tests")
                suggestions.append("pytest --cov - generate coverage report")
        elif "docker" in command:
            suggestions.append("docker system prune - clean up unused data")
            suggestions.append("docker images - list all images")

        # 通用建议
        if result.status == ToolStatus.FAILED:
            suggestions.insert(0, "Check command syntax and permissions")
            suggestions.insert(1, "Run with --verbose for more details")

        return suggestions


# 文件系统工具实现


class FileSystemTool(SmartTool):
    """
    文件系统工具

    功能：
    1. 安全文件操作
    2. 路径验证
    3. 文件内容分析
    """

    def __init__(self):
        metadata = ToolMetadata(
            name="filesystem",
            type=ToolType.FILESYSTEM,
            description="Safe file system operations with path validation",
            parameters={
                "operation": {
                    "type": "str",
                    "required": True,
                    "description": "Operation type (read, write, list, etc.)",
                },
                "path": {"type": "str", "required": True, "description": "File or directory path"},
                "content": {"type": "str", "description": "Content for write operations"},
            },
            examples=["read /path/to/file", "list /path/to/dir", "write /path/to/file content"],
            priority=ToolPriority.MEDIUM,
            requires_context=True,
        )
        super().__init__(metadata)

    async def _run_command(self, **kwargs) -> ToolResult:
        """执行文件系统操作"""
        operation = kwargs["operation"]
        path = kwargs["path"]
        content = kwargs.get("content", "")

        # 验证路径安全
        if not self._is_safe_path(path):
            return ToolResult(
                command=str(kwargs),
                status=ToolStatus.FAILED,
                error="Unsafe path detected",
                duration=0.0,
            )

        start_time = time.time()
        try:
            result = None

            if operation == "read":
                result = self._read_file(path)
            elif operation == "write":
                result = self._write_file(path, content)
            elif operation == "list":
                result = self._list_directory(path)
            else:
                return ToolResult(
                    command=str(kwargs),
                    status=ToolStatus.FAILED,
                    error=f"Unsupported operation: {operation}",
                    duration=time.time() - start_time,
                )

            return ToolResult(
                command=str(kwargs),
                status=ToolStatus.SUCCESS,
                output=result.get("output", ""),
                error=result.get("error", ""),
                duration=time.time() - start_time,
                context={"operation": operation, "path": path},
            )
        except Exception as e:
            return ToolResult(
                command=str(kwargs),
                status=ToolStatus.FAILED,
                error=str(e),
                duration=time.time() - start_time,
            )

    def _is_safe_path(self, path: str) -> bool:
        """检查路径是否安全"""
        # 禁止绝对路径，除非在允许的目录内
        safe_dirs = [os.getcwd(), os.path.expanduser("~")]

        abs_path = os.path.abspath(path)
        return any(abs_path.startswith(safe_dir) for safe_dir in safe_dirs)

    def _read_file(self, path: str) -> dict[str, str]:
        """读取文件"""
        try:
            with open(path, encoding="utf-8") as f:
                content = f.read()
            return {"output": content}
        except Exception as e:
            return {"error": str(e)}

    def _write_file(self, path: str, content: str) -> dict[str, str]:
        """写入文件"""
        try:
            # 确保目录存在
            os.makedirs(os.path.dirname(path), exist_ok=True)

            with open(path, "w", encoding="utf-8") as f:
                f.write(content)
            return {"output": f"Wrote {len(content)} characters to {path}"}
        except Exception as e:
            return {"error": str(e)}

    def _list_directory(self, path: str) -> dict[str, str]:
        """列出目录内容"""
        try:
            items = []
            for item in os.listdir(path):
                item_path = os.path.join(path, item)
                is_dir = os.path.isdir(item_path)
                size = os.path.getsize(item_path) if not is_dir else 0
                items.append({"name": item, "type": "dir" if is_dir else "file", "size": size})
            return {"output": json.dumps(items, indent=2)}
        except Exception as e:
            return {"error": str(e)}


# 工具执行器


class ToolExecutor:
    """
    工具执行器

    功能：
    1. 工具注册与管理
    2. 智能工具选择
    3. 并行工具执行
    4. 上下文传递
    """

    def __init__(self):
        self.tools: dict[str, SmartTool] = {}
        self.register_default_tools()

    def register_tool(self, tool: SmartTool):
        """注册工具"""
        self.tools[tool.metadata.name] = tool

    def register_default_tools(self):
        """注册默认工具"""
        self.register_tool(GitTool())
        self.register_tool(TerminalTool())
        self.register_tool(FileSystemTool())

    async def execute_tool(self, tool_name: str, **kwargs) -> ToolResult:
        """执行工具"""
        tool = self.tools.get(tool_name)
        if not tool:
            return ToolResult(
                command=str(kwargs),
                status=ToolStatus.FAILED,
                error=f"Tool not found: {tool_name}",
                duration=0.0,
            )
        return await tool.execute(**kwargs)

    async def execute_all(self, tools: list[tuple[str, dict[str, Any]]]) -> list[ToolResult]:
        """并行执行多个工具"""
        tasks = [self.execute_tool(tool_name, **kwargs) for tool_name, kwargs in tools]
        return await asyncio.gather(*tasks)

    def get_tool_suggestions(self, context: str) -> list[str]:
        """基于上下文生成工具建议"""
        suggestions = []

        # Git 相关建议
        if "git" in context.lower() or "repository" in context.lower():
            suggestions.append("git status")
            suggestions.append("git log -n 5")
            suggestions.append("git diff")

        # 依赖管理建议
        if any(kw in context.lower() for kw in ["install", "dependencies", "package"]):
            suggestions.append("npm install")
            suggestions.append("pip install -r requirements.txt")
            suggestions.append("brew install")

        # 测试建议
        if "test" in context.lower():
            suggestions.append("pytest")
            suggestions.append("npm test")
            suggestions.append("go test")

        return suggestions


# 便捷函数
def create_tool_executor() -> ToolExecutor:
    """创建工具执行器"""
    return ToolExecutor()


class CodeCompletionTool:
    """代码补全工具类"""

    def __init__(self):
        self.completion_cache = {}

    def complete_code(self, context: str, prefix: str, language: str = "python") -> dict[str, Any]:
        """提供代码补全建议"""
        try:
            # 基于上下文的简单补全逻辑
            completions = []

            # 根据前缀提供补全建议
            if prefix.startswith("def "):
                completions.extend(
                    [
                        "def function_name():",
                        "def function_name(parameters):",
                        "def function_name(parameters) -> ReturnType:",
                    ]
                )
            elif prefix.startswith("class "):
                completions.extend(
                    [
                        "class ClassName:",
                        "class ClassName(BaseClass):",
                        "class ClassName(metaclass=MetaClass):",
                    ]
                )
            elif prefix.startswith("if "):
                completions.extend(
                    ["if condition:", "if condition:", "if condition:", "elif condition:", "else:"]
                )
            elif prefix.startswith("for "):
                completions.extend(
                    [
                        "for item in iterable:",
                        "for i, item in enumerate(iterable):",
                        "for key, value in dictionary.items():",
                    ]
                )
            elif prefix.startswith("while "):
                completions.extend(["while condition:", "while True:"])
            elif language == "python" and prefix.endswith("."):
                # 属性或方法补全
                completions.extend(
                    [
                        "append(",
                        "extend(",
                        "insert(",
                        "remove(",
                        "pop(",
                        "clear(",
                        "index(",
                        "count(",
                        "sort(",
                        "reverse(",
                        "copy(",
                        "len(",
                        "print(",
                        "str(",
                        "int(",
                        "float(",
                        "list(",
                        "dict(",
                    ]
                )

            # 通用补全建议
            if not completions:
                completions = [
                    "pass",
                    "print(",
                    "return",
                    "import ",
                    "from ",
                    "try:",
                    "except:",
                    "finally:",
                ]

            return {
                "success": True,
                "completions": completions[:5],  # 返回前5个建议
                "context": context,
                "prefix": prefix,
            }
        except Exception as e:
            return {"success": False, "error": str(e), "completions": []}

    def suggest_function(self, docstring: str) -> list[str]:
        """根据函数文档字符串提供函数名建议"""
        # 简单的基于文档的函数名建议
        suggestions = []

        # 支持中文关键词
        if "calculate" in docstring.lower() or "计算" in docstring:
            suggestions.extend(["calculate_sum", "calculate_average", "calculate_total"])
        elif "create" in docstring.lower() or "创建" in docstring:
            suggestions.extend(["create_object", "create_instance", "create_file"])
        elif "find" in docstring.lower() or "查找" in docstring or "搜索" in docstring:
            suggestions.extend(["find_item", "find_index", "find_match"])
        elif "process" in docstring.lower() or "处理" in docstring:
            suggestions.extend(["process_data", "process_request", "process_input"])
        elif "sum" in docstring.lower() or "总和" in docstring:
            suggestions.extend(["calculate_sum", "compute_total", "add_numbers"])

        return suggestions[:3]  # 返回前3个建议


# 全局实例
code_completion_tool = CodeCompletionTool()

if __name__ == "__main__":
    # 测试
    async def test_git_tool():
        print("=== Testing Git Tool ===")
        git_tool = GitTool()

        # 测试 git status
        result = await git_tool.execute(command="status", path=".")
        print(f"Status: {result.status.value}")
        print(f"Output: {result.output[:200]}..." if result.output else "No output")
        print(f"Suggestions: {', '.join(result.suggestions[:3])}")

        # 测试 git log
        result = await git_tool.execute(command="log -n 3", path=".")
        print(f"\nStatus: {result.status.value}")
        print(f"Output: {result.output[:200]}..." if result.output else "No output")

        # 打印统计
        stats = git_tool.get_stats()
        print(f"\nStats: {json.dumps(stats, indent=2)}")

    async def test_terminal_tool():
        print("\n=== Testing Terminal Tool ===")
        terminal_tool = TerminalTool()

        # 测试 ls
        result = await terminal_tool.execute(command="ls -la", path=".")
        print(f"Status: {result.status.value}")
        print(f"Output: {result.output[:200]}..." if result.output else "No output")
        print(f"Suggestions: {', '.join(result.suggestions[:3])}")

        # 测试 npm install
        if os.path.exists("package.json"):
            result = await terminal_tool.execute(command="npm install", path=".")
            print(f"\nStatus: {result.status.value}")
            print(f"Output: {result.output[:200]}..." if result.output else "No output")

        # 打印统计
        stats = terminal_tool.get_stats()
        print(f"\nStats: {json.dumps(stats, indent=2)}")

    async def test_all_tools():
        print("\n=== Testing Tool Executor ===")
        executor = create_tool_executor()

        # 并行执行多个工具
        results = await executor.execute_all(
            [("git", {"command": "status"}), ("terminal", {"command": "ls -la"})]
        )

        for i, result in enumerate(results):
            print(f"\nTool {i + 1}:")
            print(f"Name: {result.command}")
            print(f"Status: {result.status.value}")
            print(f"Output: {result.output[:100]}..." if result.output else "No output")

    # 运行测试
    asyncio.run(test_git_tool())
    asyncio.run(test_terminal_tool())
    asyncio.run(test_all_tools())

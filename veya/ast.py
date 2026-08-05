"""
AST 解析与代码理解模块 - P1 核心能力
功能：AST 解析、符号索引、依赖分析、语义搜索
"""

from __future__ import annotations

import ast
import hashlib
import json
import os
import re
import time
from collections import defaultdict
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any


@dataclass
class Symbol:
    """代码符号定义"""

    name: str
    type: str  # 'function', 'class', 'variable', 'import', 'method'
    file_path: str
    line: int
    column: int
    end_line: int
    end_column: int
    module: str = ""
    docstring: str | None = None
    params: list[dict[str, str]] = field(default_factory=list)
    return_type: str | None = None
    is_async: bool = False
    dependencies: set[str] = field(default_factory=set)


@dataclass
class Dependency:
    """代码依赖关系"""

    source: str
    target: str
    type: str  # 'import', 'call', 'inheritance', 'composition'
    line: int
    column: int


class ASTAnalyzer:
    """
    AST 分析器 - 提取代码结构和语义信息

    功能：
    1. 多语言 AST 解析（目前支持 Python）
    2. 符号索引构建
    3. 依赖关系分析
    4. 代码摘要生成
    5. 语义搜索支持
    """

    def __init__(self):
        self.symbols: dict[str, Symbol] = {}
        self.dependencies: list[Dependency] = []
        self.modules: dict[str, str] = {}
        self.last_scan_time: float = 0
        self._cache_file = ".veya_ast_cache.json"

    def analyze_project(self, project_path: str) -> dict[str, Any]:
        """分析整个项目"""
        start_time = time.time()

        # 检查缓存
        if self._is_cache_valid(project_path):
            self._load_cache()
        else:
            # 扫描所有 Python 文件
            self._scan_directory(Path(project_path))
            self._save_cache()

        self.last_scan_time = time.time()
        return {
            "symbol_count": len(self.symbols),
            "dependency_count": len(self.dependencies),
            "modules_count": len(self.modules),
            "scan_time": time.time() - start_time,
            "cache_valid": self._is_cache_valid(project_path),
        }

    def _scan_directory(self, directory: Path):
        """扫描目录中的所有 Python 文件"""
        for file_path in directory.rglob("*.py"):
            if self._is_ignored(file_path):
                continue
            self.analyze_file(str(file_path))

    def _is_ignored(self, file_path: Path) -> bool:
        """检查是否应该忽略此文件"""
        ignored_dirs = {"__pycache__", ".git", "venv", ".vscode", "node_modules"}
        return any(part in ignored_dirs for part in file_path.parts)

    def analyze_file(self, file_path: str) -> None:
        """分析单个 Python 文件"""
        try:
            with open(file_path, encoding="utf-8") as f:
                content = f.read()
        except Exception:
            return

        try:
            tree = ast.parse(content, filename=file_path)
            self._process_ast(tree, file_path, content)
        except Exception:
            pass

    def _process_ast(self, node: ast.AST, file_path: str, content: str):
        """递归处理 AST 节点"""
        if isinstance(node, ast.FunctionDef):
            self._process_function(node, file_path, content)
        elif isinstance(node, ast.ClassDef):
            self._process_class(node, file_path, content)
        elif isinstance(node, ast.Import):
            self._process_import(node, file_path)
        elif isinstance(node, ast.ImportFrom):
            self._process_import_from(node, file_path)

        # 递归处理子节点
        for child in ast.iter_child_nodes(node):
            self._process_ast(child, file_path, content)

    def _process_function(self, node: ast.FunctionDef, file_path: str, content: str):
        """处理函数定义"""
        # 获取文档字符串
        docstring = ast.get_docstring(node) or None

        # 参数信息
        params = []
        for arg in node.args.args:
            param = {"name": arg.arg, "type": None, "default": None}
            if arg.annotation:
                param["type"] = self._get_annotation_str(arg.annotation)
            params.append(param)

        # 返回类型
        return_type = None
        if node.returns:
            return_type = self._get_annotation_str(node.returns)

        # 创建符号
        symbol = Symbol(
            name=node.name,
            type="function",
            file_path=file_path,
            line=node.lineno,
            column=node.col_offset,
            end_line=node.end_lineno or node.lineno,
            end_column=node.end_col_offset or 0,
            docstring=docstring,
            params=params,
            return_type=return_type,
            is_async=isinstance(node, ast.AsyncFunctionDef),
        )
        self.symbols[f"{file_path}:{node.name}"] = symbol

        # 检测依赖
        self._detect_dependencies(node, file_path)

    def _process_class(self, node: ast.ClassDef, file_path: str, content: str):
        """处理类定义"""
        docstring = ast.get_docstring(node) or None

        # 创建类符号
        symbol = Symbol(
            name=node.name,
            type="class",
            file_path=file_path,
            line=node.lineno,
            column=node.col_offset,
            end_line=node.end_lineno or node.lineno,
            end_column=node.end_col_offset or 0,
            docstring=docstring,
        )
        self.symbols[f"{file_path}:{node.name}"] = symbol

        # 处理方法
        for child in node.body:
            if isinstance(child, ast.FunctionDef):
                self._process_method(child, node.name, file_path, content)

        # 检测继承关系
        for base in node.bases:
            if isinstance(base, ast.Name):
                self.dependencies.append(
                    Dependency(
                        source=f"{file_path}:{node.name}",
                        target=base.id,
                        type="inheritance",
                        line=node.lineno,
                        column=node.col_offset,
                    )
                )

    def _process_method(self, node: ast.FunctionDef, class_name: str, file_path: str, content: str):
        """处理类方法"""
        # 类似函数处理，但类型为'method'
        docstring = ast.get_docstring(node) or None

        params = []
        for arg in node.args.args:
            if arg.arg == "self":  # 跳过 self
                continue
            param = {"name": arg.arg, "type": None, "default": None}
            if arg.annotation:
                param["type"] = self._get_annotation_str(arg.annotation)
            params.append(param)

        return_type = None
        if node.returns:
            return_type = self._get_annotation_str(node.returns)

        symbol = Symbol(
            name=f"{class_name}.{node.name}",
            type="method",
            file_path=file_path,
            line=node.lineno,
            column=node.col_offset,
            end_line=node.end_lineno or node.lineno,
            end_column=node.end_col_offset or 0,
            docstring=docstring,
            params=params,
            return_type=return_type,
            is_async=isinstance(node, ast.AsyncFunctionDef),
        )
        self.symbols[f"{file_path}:{class_name}.{node.name}"] = symbol

    def _process_import(self, node: ast.Import, file_path: str):
        """处理 import 语句"""
        for alias in node.names:
            self.dependencies.append(
                Dependency(
                    source=file_path,
                    target=alias.name,
                    type="import",
                    line=node.lineno,
                    column=node.col_offset,
                )
            )
            self.modules[alias.name] = alias.asname or alias.name

    def _process_import_from(self, node: ast.ImportFrom, file_path: str):
        """处理 from ... import 语句"""
        module = node.module or ""
        for alias in node.names:
            full_name = f"{module}.{alias.name}"
            self.dependencies.append(
                Dependency(
                    source=file_path,
                    target=full_name,
                    type="import",
                    line=node.lineno,
                    column=node.col_offset,
                )
            )
            self.modules[alias.name] = f"{module}.{alias.asname or alias.name}"

    def _detect_dependencies(self, node: ast.AST, file_path: str):
        """检测函数内部的依赖关系"""
        # 简单版本：查找函数调用
        for child in ast.walk(node):
            if isinstance(child, ast.Call) and isinstance(child.func, ast.Name):
                self.dependencies.append(
                    Dependency(
                        source=f"{file_path}:{node.name}",
                        target=child.func.id,
                        type="call",
                        line=child.lineno,
                        column=child.col_offset,
                    )
                )

    def _get_annotation_str(self, annotation: ast.AST) -> str:
        """获取注解字符串表示"""
        if isinstance(annotation, ast.Name):
            return annotation.id
        elif isinstance(annotation, ast.Attribute):
            return f"{self._get_annotation_str(annotation.value)}.{annotation.attr}"
        elif isinstance(annotation, ast.Subscript):
            value = self._get_annotation_str(annotation.value)
            slice_val = self._get_annotation_str(annotation.slice)
            return f"{value}[{slice_val}]"
        return "Any"

    def get_symbol(self, name: str, file_path: str | None = None) -> Symbol | None:
        """获取符号定义"""
        if file_path:
            key = f"{file_path}:{name}"
            return self.symbols.get(key)

        # 搜索所有文件
        for _key, symbol in self.symbols.items():
            if symbol.name == name:
                return symbol
        return None

    def find_references(self, symbol_name: str) -> list[dict[str, Any]]:
        """查找符号的所有引用"""
        references = []

        # 简单实现：遍历所有文件
        for file_path in set(dep.source for dep in self.dependencies):
            try:
                with open(file_path, encoding="utf-8") as f:
                    content = f.read()

                # 查找所有出现
                pattern = re.compile(rf"\b{re.escape(symbol_name)}\b")
                for match in pattern.finditer(content):
                    line_num = content.count("\n", 0, match.start()) + 1
                    col = match.start() - content.rfind("\n", 0, match.start())
                    references.append(
                        {
                            "file": file_path,
                            "line": line_num,
                            "column": col,
                            "context": content[max(0, match.start() - 20) : match.end() + 20],
                        }
                    )
            except Exception:
                pass

        return references

    def get_dependencies(self, symbol_name: str) -> list[Dependency]:
        """获取符号的依赖关系"""
        return [dep for dep in self.dependencies if dep.source == symbol_name]

    def get_callers(self, symbol_name: str) -> list[Dependency]:
        """获取调用此符号的所有地方"""
        return [dep for dep in self.dependencies if dep.target == symbol_name]

    def get_call_graph(self) -> dict[str, list[str]]:
        """获取调用图"""
        graph = defaultdict(list)
        for dep in self.dependencies:
            if dep.type == "call":
                graph[dep.source].append(dep.target)
        return dict(graph)

    def search_by_signature(self, signature: str) -> list[Symbol]:
        """通过函数签名搜索符号"""
        results = []
        for symbol in self.symbols.values():
            if symbol.type in ["function", "method"]:
                sig = self._build_signature(symbol)
                if signature.lower() in sig.lower():
                    results.append(symbol)
        return results

    def _build_signature(self, symbol: Symbol) -> str:
        """构建函数签名字符串"""
        parts = []
        for p in symbol.params:
            if p["type"]:
                parts.append(f"{p['name']}: {p['type']}")
            else:
                parts.append(p["name"])
        params_str = ", ".join(parts)
        return_str = f" -> {symbol.return_type}" if symbol.return_type else ""
        return f"def {symbol.name}({params_str}){return_str}"

    def _cache_key(self, project_path: str) -> str:
        """生成缓存键"""
        return hashlib.md5(project_path.encode()).hexdigest()

    def _is_cache_valid(self, project_path: str) -> bool:
        """检查缓存是否有效"""
        if not os.path.exists(self._cache_file):
            return False

        try:
            with open(self._cache_file) as f:
                cache = json.load(f)

            # 检查缓存是否针对当前项目
            if cache.get("cache_key") != self._cache_key(project_path):
                return False

            # 检查缓存时间
            cache_time = cache.get("timestamp", 0)
            return time.time() - cache_time <= 3600  # 1小时过期
        except Exception:
            return False

    def _save_cache(self):
        """保存缓存"""

        def _serialize(obj):
            """Convert sets to lists for JSON serialization."""
            if isinstance(obj, set):
                return list(obj)
            if isinstance(obj, dict):
                return {k: _serialize(v) for k, v in obj.items()}
            if isinstance(obj, (list, tuple)):
                return [_serialize(v) for v in obj]
            return obj

        cache = {
            "cache_key": self._cache_key("."),
            "timestamp": time.time(),
            "symbols": {k: _serialize(v.__dict__) for k, v in self.symbols.items()},
            "dependencies": [d.__dict__ for d in self.dependencies],
            "modules": self.modules,
        }
        with open(self._cache_file, "w") as f:
            json.dump(cache, f)

    def _load_cache(self):
        """加载缓存"""
        try:
            with open(self._cache_file) as f:
                cache = json.load(f)

            self.symbols = {}
            for k, v in cache.get("symbols", {}).items():
                # 重新创建 Symbol 对象
                symbol = Symbol(
                    name=v["name"],
                    type=v["type"],
                    file_path=v["file_path"],
                    line=v["line"],
                    column=v["column"],
                    end_line=v["end_line"],
                    end_column=v["end_column"],
                    module=v.get("module", ""),
                    docstring=v.get("docstring"),
                    params=v.get("params", []),
                    return_type=v.get("return_type"),
                    is_async=v.get("is_async", False),
                    dependencies=set(v.get("dependencies", [])),
                )
                self.symbols[k] = symbol

            self.dependencies = []
            for d in cache.get("dependencies", []):
                self.dependencies.append(
                    Dependency(
                        source=d["source"],
                        target=d["target"],
                        type=d["type"],
                        line=d["line"],
                        column=d["column"],
                    )
                )

            self.modules = cache.get("modules", {})
        except Exception:
            self.symbols = {}
            self.dependencies = []
            self.modules = {}

    def predict_relevant_files(
        self, query: str, all_files: list[str], max_files: int = 5
    ) -> list[str]:
        """基于 AST 符号和查询预测相关文件"""
        query_lower = query.lower()

        # 首先检查是否有 AST 符号匹配查询
        matched_files = set()
        for symbol in self.symbols.values():
            if symbol.name.lower() in query_lower and symbol.file_path in all_files:
                matched_files.add(symbol.file_path)

        if matched_files:
            return list(matched_files)[:max_files]

        # 否则使用基于关键词的启发式匹配
        relevant = []
        keywords = ["config", "main", "app", "api", "service", "model", "view", "controller"]

        for file_path in all_files:
            file_lower = file_path.lower()

            if any(kw in file_lower for kw in ["test", "spec"]):
                continue

            if (
                (
                    any(kw in query_lower for kw in ["api", "endpoint", "route"])
                    and any(kw in file_lower for kw in ["api", "route"])
                )
                or (
                    any(kw in query_lower for kw in ["database", "db", "sql"])
                    and any(kw in file_lower for kw in ["db", "model"])
                )
                or (
                    any(kw in query_lower for kw in ["auth", "login", "user"])
                    and any(kw in file_lower for kw in ["auth", "user"])
                )
                or any(kw in file_lower for kw in keywords)
            ):
                relevant.append(file_path)

        # 去重并限制数量
        seen = set()
        unique_files = []
        for f in relevant:
            if f not in seen:
                seen.add(f)
                unique_files.append(f)

        return unique_files[:max_files]

    def generate_code_summary(self, file_path: str) -> dict[str, Any]:
        """生成代码摘要"""
        # 获取文件中的所有符号
        symbols = [s for s in self.symbols.values() if s.file_path == file_path]

        # 按类型分类
        functions = [s for s in symbols if s.type == "function"]
        classes = [s for s in symbols if s.type == "class"]

        return {
            "file": file_path,
            "function_count": len(functions),
            "class_count": len(classes),
            "symbol_count": len(symbols),
            "functions": [
                {
                    "name": f.name,
                    "line": f.line,
                    "docstring": f.docstring[:100] + "..."
                    if f.docstring and len(f.docstring) > 100
                    else f.docstring,
                }
                for f in functions[:10]
            ],
            "classes": [
                {
                    "name": c.name,
                    "line": c.line,
                    "method_count": len(
                        [
                            s
                            for s in symbols
                            if s.type == "method" and s.name.startswith(f"{c.name}.")
                        ]
                    ),
                }
                for c in classes
            ],
        }


# 便捷函数

def _def_signature(node: ast.AST) -> str:
    """函数/方法 → 单行签名(上下文压缩核心)。"""
    try:
        args = ast.unparse(node.args)
    except Exception:
        args = "..."
    ret = ""
    if getattr(node, "returns", None) is not None:
        try:
            ret = f" -> {ast.unparse(node.returns)}"
        except Exception:
            ret = ""
    doc = ast.get_docstring(node) or ""
    first_line = doc.splitlines()[0][:80] if doc else ""
    sig = (
        f"def {node.name}({args}){ret}  # L{node.lineno}-{getattr(node, 'end_lineno', node.lineno)}"
    )
    return f'{sig}  """{first_line}"""' if first_line else sig


def extract_skeleton(source: str, filepath: str, max_chars: int = 8000) -> str:
    """上下文压缩: 只返回 AST 骨架(签名/行号/docstring 首行),防止 Token 爆炸。

    Master Tool Registry 的 read_file_ast / 认知引擎的 read_skeleton 共用此实现。
    """
    try:
        tree = ast.parse(source)
    except SyntaxError as exc:
        return f"# {filepath}: SYNTAX ERROR {exc}"

    lines = [f"# skeleton: {filepath}"]
    for node in tree.body:
        if isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef)):
            lines.append(_def_signature(node))
        elif isinstance(node, ast.ClassDef):
            bases = ""
            if node.bases:
                try:
                    bases = f"({', '.join(ast.unparse(b) for b in node.bases)}"
                except Exception:
                    bases = ""
            doc = ast.get_docstring(node) or ""
            head = f"class {node.name}{bases}  # L{node.lineno}-{getattr(node, 'end_lineno', node.lineno)}"
            lines.append(f'{head}  """{doc.splitlines()[0][:80]}"""' if doc else head)
            for item in node.body:
                if isinstance(item, (ast.FunctionDef, ast.AsyncFunctionDef)):
                    lines.append("    " + _def_signature(item))
        elif isinstance(node, ast.Assign) and node.targets:
            names = [t.id for t in node.targets if isinstance(t, ast.Name)]
            if names:
                lines.append(f"{', '.join(names)} = ...")
    return "\n".join(lines)[:max_chars]


def create_ast_analyzer() -> ASTAnalyzer:
    """创建 AST 分析器"""
    return ASTAnalyzer()


if __name__ == "__main__":
    # 测试
    analyzer = create_ast_analyzer()
    project_path = "."

    print(f"Analyzing project: {project_path}")
    stats = analyzer.analyze_project(project_path)
    print(f"Analysis complete: {json.dumps(stats, indent=2)}")

    # 显示一些统计信息
    print("\nTop 5 functions:")
    functions = [s for s in analyzer.symbols.values() if s.type == "function"]
    for func in functions[:5]:
        print(f"  {func.name} ({func.file_path}:{func.line})")

    # 生成代码摘要
    if functions:
        summary = analyzer.generate_code_summary(functions[0].file_path)
        print(f"\nCode summary for {summary['file']}:")
        print(f"  Functions: {summary['function_count']}")
        print(f"  Classes: {summary['class_count']}")

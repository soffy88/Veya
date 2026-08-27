"""
跨语言支持模块 - P3 核心能力
功能：支持 Java、C++、Rust 等多语言代码理解与生成
"""

from __future__ import annotations

import re
from dataclasses import dataclass, field
from enum import StrEnum
from pathlib import Path
from typing import Any


class Language(StrEnum):
    """支持的语言枚举"""

    PYTHON = "python"
    JAVA = "java"
    CPP = "cpp"
    RUST = "rust"
    JAVASCRIPT = "javascript"
    TYPESCRIPT = "typescript"
    GO = "go"


@dataclass
class LanguageFeature:
    """语言特性"""

    name: str
    supported_versions: list[str]
    extensions: list[str]
    metadata: dict[str, Any] = field(default_factory=dict)


@dataclass
class CrossLanguageResult:
    """跨语言处理结果"""

    source_language: Language
    target_language: Language
    source_code: str
    target_code: str
    mapping: dict[str, str]
    confidence: float
    warnings: list[str] = field(default_factory=list)


class LanguageParser:
    """
    多语言解析器基类
    """

    def __init__(self, language: Language):
        self.language = language

    def parse_file(self, file_path: str) -> dict[str, Any]:
        """解析文件"""
        raise NotImplementedError

    def extract_functions(self, code: str) -> list[dict[str, Any]]:
        """提取函数"""
        raise NotImplementedError

    def extract_classes(self, code: str) -> list[dict[str, Any]]:
        """提取类"""
        raise NotImplementedError

    def detect_patterns(self, code: str) -> list[str]:
        """检测代码模式"""
        raise NotImplementedError


class PythonParser(LanguageParser):
    """Python 解析器"""

    def __init__(self):
        super().__init__(Language.PYTHON)

    def parse_file(self, file_path: str) -> dict[str, Any]:
        try:
            with open(file_path, encoding="utf-8") as f:
                content = f.read()
            return {
                "functions": self.extract_functions(content),
                "classes": self.extract_classes(content),
                "imports": self.extract_imports(content),
                "patterns": self.detect_patterns(content),
            }
        except Exception as e:
            return {"error": str(e)}

    def extract_functions(self, code: str) -> list[dict[str, Any]]:
        functions = []
        pattern = r"def\s+(\w+)\s*\((.*?)\)\s*(?:->\s*(\w+))?:\s*"
        for match in re.finditer(pattern, code, re.MULTILINE):
            functions.append(
                {
                    "name": match.group(1),
                    "params": match.group(2).split(",") if match.group(2) else [],
                    "return_type": match.group(3),
                    "line": code.count("\n", 0, match.start()) + 1,
                }
            )
        return functions

    def extract_classes(self, code: str) -> list[dict[str, Any]]:
        classes = []
        pattern = r"class\s+(\w+)\s*(?:\((.*?)\))?:"
        for match in re.finditer(pattern, code, re.MULTILINE):
            classes.append(
                {
                    "name": match.group(1),
                    "base_classes": match.group(2).split(",") if match.group(2) else [],
                    "line": code.count("\n", 0, match.start()) + 1,
                }
            )
        return classes

    def extract_imports(self, code: str) -> list[str]:
        imports = []
        pattern = r"(?:from\s+(\S+)\s+import|import\s+(\S+))"
        for match in re.finditer(pattern, code):
            imports.append(match.group(1) or match.group(2))
        return imports

    def detect_patterns(self, code: str) -> list[str]:
        patterns = []
        # 检测装饰器模式
        if "@" in code:
            patterns.append("decorator_pattern")
        # 检测生成器
        if "yield" in code:
            patterns.append("generator_pattern")
        # 检测异步
        if "async" in code:
            patterns.append("async_pattern")
        return patterns


class JavaParser(LanguageParser):
    """Java 解析器"""

    def __init__(self):
        super().__init__(Language.JAVA)

    def parse_file(self, file_path: str) -> dict[str, Any]:
        try:
            with open(file_path, encoding="utf-8") as f:
                content = f.read()
            return {
                "functions": self.extract_methods(content),
                "classes": self.extract_classes(content),
                "imports": self.extract_imports(content),
                "patterns": self.detect_patterns(content),
            }
        except Exception as e:
            return {"error": str(e)}

    def extract_methods(self, code: str) -> list[dict[str, Any]]:
        methods = []
        pattern = r"(public|private|protected)\s+(\w+(?:<\w+>)?)\s+(\w+)\s*\((.*?)\)\s*\{"
        for match in re.finditer(pattern, code, re.MULTILINE):
            methods.append(
                {
                    "modifier": match.group(1),
                    "return_type": match.group(2),
                    "name": match.group(3),
                    "params": match.group(4),
                    "line": code.count("\n", 0, match.start()) + 1,
                }
            )
        return methods

    def extract_classes(self, code: str) -> list[dict[str, Any]]:
        classes = []
        pattern = (
            r"(?:public\s+)?class\s+(\w+)\s*(?:extends\s+(\w+))?\s*(?:implements\s+(.*?))?\s*\{"
        )
        for match in re.finditer(pattern, code, re.MULTILINE):
            classes.append(
                {
                    "name": match.group(1),
                    "extends": match.group(2),
                    "implements": match.group(3),
                    "line": code.count("\n", 0, match.start()) + 1,
                }
            )
        return classes

    def extract_imports(self, code: str) -> list[str]:
        imports = []
        pattern = r"import\s+([\w\.]+(?:\.[\w]+)*);"
        for match in re.finditer(pattern, code):
            imports.append(match.group(1))
        return imports

    def detect_patterns(self, code: str) -> list[str]:
        patterns = []
        # 检测设计模式
        if "interface" in code and "implements" in code:
            patterns.append("interface_pattern")
        if "abstract" in code:
            patterns.append("abstract_class_pattern")
        if "@Override" in code:
            patterns.append("override_pattern")
        return patterns


class CrossLanguageTranslator:
    """
    跨语言翻译器

    功能：
    1. 语言特性映射
    2. 代码转换
    3. 语法适配
    4. 库/框架适配
    """

    # Project analysis is used by the P3 workflow and is often pointed at the
    # repository root. These trees are generated dependencies/caches rather
    # than project source; traversing them makes an otherwise small query
    # proportional to the local virtualenv or frontend install size.
    _ANALYSIS_EXCLUDED_DIRS = frozenset(
        {
            ".git",
            ".hg",
            ".svn",
            ".venv",
            "venv",
            "env",
            "node_modules",
            "__pycache__",
            ".pytest_cache",
            ".mypy_cache",
            ".ruff_cache",
            ".tox",
            ".svelte-kit",
            ".next",
            ".cache",
            ".parcel-cache",
            ".turbo",
            ".pnpm-store",
            ".code-review-graph",
            ".veya",
            ".veya-project",
            "coverage",
            "htmlcov",
            "ms-playwright",
            "build",
            "dist",
        }
    )

    def __init__(self):
        self.parsers = {
            Language.PYTHON: PythonParser(),
            Language.JAVA: JavaParser(),
            # 可以添加更多语言解析器
        }
        self.translation_rules = self._build_translation_rules()

    def _build_translation_rules(self) -> dict[str, dict[str, Any]]:
        """构建翻译规则"""
        return {
            "python_to_java": {
                "def": "public void",
                "self": "this",
                "print": "System.out.println",
                "list": "ArrayList",
                "dict": "HashMap",
            },
            "java_to_python": {
                "public void": "def",
                "this": "self",
                "System.out.println": "print",
                "ArrayList": "list",
                "HashMap": "dict",
            },
        }

    def translate(
        self, source_code: str, source_lang: Language, target_lang: Language
    ) -> CrossLanguageResult:
        """翻译代码"""
        if source_lang == target_lang:
            return CrossLanguageResult(
                source_language=source_lang,
                target_language=target_lang,
                source_code=source_code,
                target_code=source_code,
                mapping={},
                confidence=1.0,
                warnings=["Source and target languages are the same"],
            )

        # 获取翻译规则
        rule_key = f"{source_lang.value}_to_{target_lang.value}"
        rules = self.translation_rules.get(rule_key, {})

        # 应用翻译
        translated_code = source_code
        mapping = {}

        for source_pattern, target_pattern in rules.items():
            if source_pattern in source_code:
                translated_code = translated_code.replace(source_pattern, target_pattern)
                mapping[source_pattern] = target_pattern

        # 语言特定处理
        if source_lang == Language.PYTHON and target_lang == Language.JAVA:
            translated_code = self._python_to_java(translated_code)
        elif source_lang == Language.JAVA and target_lang == Language.PYTHON:
            translated_code = self._java_to_python(translated_code)

        # 计算置信度
        confidence = self._calculate_confidence(
            source_code, translated_code, source_lang, target_lang
        )

        return CrossLanguageResult(
            source_language=source_lang,
            target_language=target_lang,
            source_code=source_code,
            target_code=translated_code,
            mapping=mapping,
            confidence=confidence,
            warnings=self._generate_warnings(source_code, translated_code),
        )

    def _python_to_java(self, code: str) -> str:
        """Python 到 Java 转换"""
        # 添加类包装
        if "def " in code and "class " not in code:
            class_name = "GeneratedClass"
            code = f"public class {class_name} {{\n    {code}\n}}"

        # 转换缩进
        code = code.replace("    ", "  ")

        # 添加分号
        lines = code.split("\n")
        for i in range(len(lines)):
            line = lines[i].strip()
            if (
                line
                and not line.endswith("{")
                and not line.endswith("}")
                and not line.endswith(";")
            ):
                lines[i] = lines[i].rstrip() + ";"
        code = "\n".join(lines)

        return code

    def _java_to_python(self, code: str) -> str:
        """Java 到 Python 转换"""
        # 移除类包装
        if "public class" in code:
            # 提取类内容
            match = re.search(r"public class \w+ \{([^}]+)\}", code, re.DOTALL)
            if match:
                code = match.group(1).strip()

        # 移除分号
        code = code.replace(";", "")

        # 转换 this 到 self
        code = code.replace("this.", "self.")

        # 转换访问修饰符
        code = re.sub(r"(public|private|protected)\s+", "", code)

        return code

    def _calculate_confidence(
        self, source: str, target: str, source_lang: Language, target_lang: Language
    ) -> float:
        """计算翻译置信度"""
        confidence = 0.5  # 基础置信度

        # 根据匹配规则调整
        rule_key = f"{source_lang.value}_to_{target_lang.value}"
        rules = self.translation_rules.get(rule_key, {})

        matched_rules = 0
        for source_pattern in rules:
            if source_pattern in source:
                matched_rules += 1

        if rules:
            confidence += (matched_rules / len(rules)) * 0.3

        # 根据目标语言语法检查
        if target_lang == Language.JAVA:
            if "{" in target and "}" in target:
                confidence += 0.1
            if ";" in target:
                confidence += 0.1
        elif target_lang == Language.PYTHON:
            if "def " in target:
                confidence += 0.1
            if "class " in target:
                confidence += 0.1

        return min(confidence, 1.0)

    def _generate_warnings(self, source: str, target: str) -> list[str]:
        """生成警告"""
        warnings = []

        if len(source) > 1000 and len(target) < 500:
            warnings.append("Translation may have lost content")

        if "TODO" in source or "FIXME" in source:
            warnings.append("Source contains TODOs/FIXMEs")

        return warnings

    def analyze_project(self, project_path: str) -> dict[str, Any]:
        """分析项目中的多语言文件"""
        import os

        language_stats = {}
        file_extensions = {
            ".py": Language.PYTHON,
            ".java": Language.JAVA,
            ".js": Language.JAVASCRIPT,
            ".ts": Language.TYPESCRIPT,
            ".cpp": Language.CPP,
            ".rs": Language.RUST,
            ".go": Language.GO,
        }

        for root, dirs, files in os.walk(project_path):
            # ``os.walk`` honours in-place pruning before descending. Keep
            # source directories such as ``src`` and ``tests`` intact while
            # avoiding dependency/build trees that can contain millions of
            # files.
            dirs[:] = [
                directory for directory in dirs if directory not in self._ANALYSIS_EXCLUDED_DIRS
            ]
            for file in files:
                ext = Path(file).suffix
                if ext in file_extensions:
                    lang = file_extensions[ext]
                    if lang not in language_stats:
                        language_stats[lang] = {"files": 0, "total_lines": 0}

                    language_stats[lang]["files"] += 1

                    # 计算行数
                    try:
                        file_path = os.path.join(root, file)
                        with open(file_path, encoding="utf-8") as f:
                            lines = len(f.readlines())
                        language_stats[lang]["total_lines"] += lines
                    except Exception:
                        pass

        return language_stats


# 便捷函数
def create_cross_language_translator() -> CrossLanguageTranslator:
    """创建跨语言翻译器"""
    return CrossLanguageTranslator()


if __name__ == "__main__":
    # 测试
    translator = create_cross_language_translator()

    # Python 到 Java 翻译
    print("=== Python to Java Translation ===")
    python_code = """
def greet(name):
    return f"Hello, {name}!"

class Person:
    def __init__(self, name):
        self.name = name

    def greet(self):
        return greet(self.name)
"""

    result = translator.translate(python_code, Language.PYTHON, Language.JAVA)

    print(f"Source (Python):\n{result.source_code}")
    print(f"\nTarget (Java):\n{result.target_code}")
    print(f"\nConfidence: {result.confidence}")
    print(f"Warnings: {result.warnings}")

    # Java 到 Python 翻译
    print("\n=== Java to Python Translation ===")
    java_code = """
public class Calculator {
    public int add(int a, int b) {
        return a + b;
    }

    public int subtract(int a, int b) {
        return a - b;
    }
}
"""

    result = translator.translate(java_code, Language.JAVA, Language.PYTHON)

    print(f"Source (Java):\n{result.source_code}")
    print(f"\nTarget (Python):\n{result.target_code}")
    print(f"\nConfidence: {result.confidence}")
    print(f"Warnings: {result.warnings}")

    # 项目分析
    print("\n=== Project Analysis ===")
    # 在当前目录分析
    stats = translator.analyze_project(".")
    for lang, data in stats.items():
        print(f"{lang.value}: {data['files']} files, {data['total_lines']} lines")

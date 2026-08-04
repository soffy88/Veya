#!/usr/bin/env python3
"""
测试hicode新功能
"""

import os
import sys

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))

from hicode.ast import ASTAnalyzer
from hicode.tools import code_completion_tool


def test_code_completion():
    """测试代码补全功能"""
    print("=== 测试代码补全功能 ===")

    # 测试函数定义补全
    result = code_completion_tool.complete_code(
        context="函数定义上下文", prefix="def ", language="python"
    )
    print("函数定义补全结果:")
    if result.get("success"):
        for i, completion in enumerate(result.get("completions", []), 1):
            print(f"  {i}. {completion}")
    else:
        print(f"  错误: {result.get('error')}")

    # 测试类定义补全
    result = code_completion_tool.complete_code(
        context="类定义上下文", prefix="class ", language="python"
    )
    print("\n类定义补全结果:")
    if result.get("success"):
        for i, completion in enumerate(result.get("completions", []), 1):
            print(f"  {i}. {completion}")
    else:
        print(f"  错误: {result.get('error')}")

    # 测试属性补全
    result = code_completion_tool.complete_code(
        context="列表对象上下文", prefix="my_list.", language="python"
    )
    print("\n列表属性补全结果:")
    if result.get("success"):
        for i, completion in enumerate(result.get("completions", []), 1):
            print(f"  {i}. {completion}")
    else:
        print(f"  错误: {result.get('error')}")


def test_code_analysis():
    """测试代码分析功能"""
    print("\n=== 测试代码分析功能 ===")

    # 创建AST分析器实例
    analyzer = ASTAnalyzer()

    sample_code = '''
def calculate_sum(numbers):
    """计算数字列表的总和"""
    total = 0
    for num in numbers:
        total += num
    return total

result = calculate_sum([1, 2, 3, 4, 5])
print(f"总和是: {result}")
'''

    try:
        # 使用analyze_file方法分析代码
        import tempfile

        with tempfile.NamedTemporaryFile(mode="w", suffix=".py", delete=False) as f:
            f.write(sample_code)
            temp_file = f.name

        analyzer.analyze_file(temp_file)

        # 获取分析结果 - 使用正确的方法
        symbol = analyzer.get_symbol("calculate_sum")
        if symbol:
            print("分析结果:")
            print(f"  函数名: {symbol.name}")
            print(f"  类型: {symbol.type}")
            print(f"  文件: {symbol.file_path}")
        else:
            print("分析结果: 未找到符号")

        # 清理临时文件
        os.unlink(temp_file)
    except Exception as e:
        print(f"分析错误: {e}")


def test_function_suggestions():
    """测试函数名建议功能"""
    print("\n=== 测试函数名建议功能 ===")

    docstring = "计算两个数的总和"
    suggestions = code_completion_tool.suggest_function(docstring)
    print(f"文档字符串: {docstring}")
    print("函数名建议:")
    for i, suggestion in enumerate(suggestions, 1):
        print(f"  {i}. {suggestion}")


if __name__ == "__main__":
    test_code_completion()
    test_code_analysis()
    test_function_suggestions()
    print("\n=== 所有测试完成 ===")

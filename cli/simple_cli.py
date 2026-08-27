#!/usr/bin/env python3
"""
简化版veya命令行工具
提供基本的代码生成和分析功能
"""

import argparse
import asyncio
import json
import os
import sys

# 添加项目路径以便导入
sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)) + "/..")

from veya.agent_collaboration import create_agent_collaborator


def _maybe_confirm(action: str, resource: str, description: str, *, persona: str = "build") -> bool:
    """G5 交互式权限确认：规则 ALLOW/DENY 自动裁决，PENDING 时询问用户。"""
    from veya.obase.authz import InteractivePermissionGate

    gate = InteractivePermissionGate()
    result = asyncio.run(gate.evaluate(action, resource=resource, persona=persona, wait=False))
    if result["decision"] == "pending":
        request_id = result.get("request_id")
        answer = input(f"⚠ 需要确认：{description}  [y/N] ").strip().lower()
        if answer in ("y", "yes"):
            if request_id:
                gate.approve(request_id, note="approved via CLI")
            return True
        if request_id:
            gate.deny(request_id, note="denied via CLI")
        return False
    return result["decision"] == "allow"


# Demo CLI: wire to real modules; graceful degradation if missing.
def _load_completion_tool():
    try:
        from veya.semantic_search import SemanticSearch

        return SemanticSearch()
    except Exception:
        return None


def _load_analyzer():
    try:
        from veya.ast import create_ast_analyzer

        return create_ast_analyzer()
    except Exception:
        return None


code_completion_tool = _load_completion_tool()
analyze_code = None
if code_completion_tool is not None:

    def analyze_code(code: str) -> dict:
        try:
            analyzer = _load_analyzer()
            if analyzer is None:
                return {"error": "AST analyzer unavailable", "results": []}
            project_path = os.path.dirname(os.path.abspath(__file__)) + "/.."
            return {"results": [], "stats": analyzer.analyze_project(project_path)}
        except Exception as e:
            return {"error": str(e), "results": []}
else:

    def analyze_code(code: str) -> dict:
        return {"error": "code analysis unavailable", "results": []}


def setup_parser():
    """设置命令行参数解析器"""
    parser = argparse.ArgumentParser(description="veya - AI编程助手", prog="veya")

    parser.add_argument("--version", action="version", version="veya 0.6.0")

    subparsers = parser.add_subparsers(dest="command", help="可用命令")

    # 代码补全命令
    complete_parser = subparsers.add_parser("complete", help="代码补全")
    complete_parser.add_argument("prefix", help="代码前缀")
    complete_parser.add_argument("--language", default="python", help="编程语言")
    complete_parser.add_argument("--context", help="上下文")

    # 代码分析命令
    analyze_parser = subparsers.add_parser("analyze", help="代码分析")
    analyze_parser.add_argument("code", nargs="?", help="要分析的代码")
    analyze_parser.add_argument("--file", help="代码文件路径")

    # 生成代码命令
    generate_parser = subparsers.add_parser("generate", help="生成代码")
    generate_parser.add_argument("description", help="代码描述")
    generate_parser.add_argument("--language", default="python", help="编程语言")
    generate_parser.add_argument("--output", help="输出文件路径")
    generate_parser.add_argument(
        "--tests", action="append", default=[], help="测试节点 (如 test_solve); 多次传入"
    )
    generate_parser.add_argument(
        "--reliable",
        action="store_true",
        help="走代码可靠性闭环 (CODE_RELIABILITY_LOOP=1 时默认开启)",
    )

    # 协作任务命令
    collaborate_parser = subparsers.add_parser("collaborate", help="协作任务")
    collaborate_parser.add_argument(
        "action", choices=["create", "assign", "complete"], help="协作动作"
    )
    collaborate_parser.add_argument("--description", help="任务描述")
    collaborate_parser.add_argument("--agent", help="代理ID")
    collaborate_parser.add_argument("--task-id", help="任务ID")

    return parser


def handle_complete(args):
    """处理代码补全命令"""
    print("=== 代码补全 ===")
    if code_completion_tool is None:
        print("代码补全不可用（semantic_search 模块加载失败）")
        return
    try:
        completions = code_completion_tool.recommend_completion(args.prefix or args.context or "")
        if completions:
            print("建议的补全:")
            for i, completion in enumerate(completions, 1):
                print(f"  {i}. {completion}")
        else:
            print("暂无补全建议")
    except Exception as e:
        print(f"补全失败: {e}")


def handle_analyze(args):
    """处理代码分析命令"""
    print("=== 代码分析 ===")

    # 获取代码内容
    if args.file:
        try:
            with open(args.file, encoding="utf-8") as f:
                code = f.read()
        except Exception as e:
            print(f"读取文件错误: {e}")
            return
    elif args.code:
        code = args.code
    else:
        print("请提供代码或文件路径")
        return

    # 分析代码
    try:
        result = analyze_code(code)
        print("分析结果:")
        print(json.dumps(result, indent=2, ensure_ascii=False))
    except Exception as e:
        print(f"分析错误: {e}")


def _sample_generate(spec, workspace=None, failure_context=None, tests=None):
    """演示用生成器 (可被可靠性闭环注入; 真实 LLM 生成可替换本函数)。"""
    if failure_context is not None:
        # 修复轮: 按失败上下文给出"修对"的示例 (演示语义)
        return {
            "main.py": "def solve():\n    return 42\n",
            "tests/test_main.py": "from main import solve\n\n"
            "def test_solve():\n    assert solve() == 42\n",
        }
    return {
        "main.py": "def solve():\n    return 0\n",
        "tests/test_main.py": "from main import solve\n\n"
        "def test_solve():\n    assert solve() == 42\n",
    }


def _generate_with_reliability(args):
    """可靠性闭环路径 (方案 A+C): generate → sandbox test → 修复轮 ≤ max_repairs。"""
    from services.code_agent_reliability import run_veya_code_agent

    print("=== 代码生成 (可靠性闭环) ===")
    result = run_veya_code_agent(
        spec=args.description,
        tests=list(args.tests) or ["test_solve"],
        workspace={},
        veya_generate=_sample_generate,
        max_repairs=int(os.environ.get("CODE_RELIABILITY_MAX_REPAIRS", "3")),
        audit_path=os.environ.get("CODE_RELIABILITY_AUDIT"),
    )
    if result.status == "merged_candidate":
        print(
            f"[ok] merged_candidate  patch={result.patch.patch_id}  修复轮数={result.repairs_used}"
        )
        for a in result.action_trace:
            print(f"  - {a['action']} (passed={a.get('passed')})")
        if args.output:
            out_files = dict(result.patch.files)
            main = out_files.get("main.py")
            if main and _maybe_confirm("write", args.output, f"写入文件 {args.output}"):
                with open(args.output, "w", encoding="utf-8") as f:
                    f.write(main)
                print(f"代码已保存到: {args.output}")
        return True
    if result.status == "clarify":
        print(f"[clarify] {result.clarify_message}")
        return False
    print(
        f"[aborted] {result.signature.summary if result.signature else '未知'} "
        f"fingerprint={result.signature.fingerprint if result.signature else '-'}"
    )
    for a in result.action_trace:
        print(f"  - {a['action']} (passed={a.get('passed')})")
    return False


def handle_generate(args):
    """处理代码生成命令"""
    # 回滚开关: CODE_RELIABILITY_LOOP=1 或 --reliable → 走可靠性闭环;
    # 否则走旧 generate-only 路径 (规格 §9 回滚)。
    use_loop = args.reliable or os.environ.get("CODE_RELIABILITY_LOOP", "0") == "1"
    if use_loop:
        _generate_with_reliability(args)
        return

    print("=== 代码生成 ===")
    print(f"描述: {args.description}")
    print(f"语言: {args.language}")

    # 这里应该调用实际的代码生成逻辑
    # 为了演示，我们返回一些示例代码
    sample_code = f'# 生成的{args.language}代码\n\ndef example_function():\n    """{args.description}"""\n    pass\n'

    if args.output:
        if not _maybe_confirm("write", args.output, f"写入文件 {args.output}"):
            print("已取消写入（权限拒绝）")
            return
        try:
            with open(args.output, "w", encoding="utf-8") as f:
                f.write(sample_code)
            print(f"代码已保存到: {args.output}")
        except Exception as e:
            print(f"保存文件错误: {e}")
    else:
        print("生成的代码:")
        print(sample_code)


def handle_collaborate(args):
    """处理协作任务命令"""
    print("=== 协作任务 ===")

    # 创建协作器
    collaborator = create_agent_collaborator()

    try:
        if args.action == "create":
            if not args.description:
                print("请提供任务描述")
                return
            task_id = collaborator.create_task(description=args.description, agent_role="planner")
            print(f"创建任务成功，任务ID: {task_id}")

        elif args.action == "assign":
            if not args.task_id or not args.agent:
                print("请提供任务ID和代理ID")
                return
            success = collaborator.assign_task(args.task_id, args.agent)
            if success:
                print(f"任务 {args.task_id} 已分配给 {args.agent}")
            else:
                print("分配任务失败")

        elif args.action == "complete":
            if not args.task_id:
                print("请提供任务ID")
                return
            success = collaborator.complete_task(args.task_id, "任务完成")
            if success:
                print(f"任务 {args.task_id} 已完成")
            else:
                print("完成任务失败")

    except Exception as e:
        print(f"协作错误: {e}")


def main():
    """主函数"""
    parser = setup_parser()
    args = parser.parse_args()

    if not args.command:
        parser.print_help()
        return

    # 根据命令执行相应操作
    if args.command == "complete":
        handle_complete(args)
    elif args.command == "analyze":
        handle_analyze(args)
    elif args.command == "generate":
        handle_generate(args)
    elif args.command == "collaborate":
        handle_collaborate(args)
    else:
        parser.print_help()


if __name__ == "__main__":
    main()

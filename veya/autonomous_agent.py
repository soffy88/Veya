"""
自主 AI 代理模块 - P3 核心能力
功能：自主规划、自我改进、记忆系统、长期学习
"""

from __future__ import annotations

import hashlib
import time
from dataclasses import dataclass, field
from enum import StrEnum
from typing import Any


class AgentGoal(StrEnum):
    """AI 代理目标类型"""

    CODE_GENERATION = "code_generation"
    PROBLEM_SOLVING = "problem_solving"
    SYSTEM_DESIGN = "system_design"
    CODE_REVIEW = "code_review"
    LEARNING = "learning"


@dataclass
class AgentMemory:
    """代理记忆单元"""

    memory_id: str
    content: str
    tags: list[str]
    created_at: float = field(default_factory=time.time)
    last_accessed: float = field(default_factory=time.time)
    access_count: int = 0
    confidence: float = 1.0  # 记忆置信度

    def to_dict(self) -> dict[str, Any]:
        return {
            "memory_id": self.memory_id,
            "content": self.content,
            "tags": self.tags,
            "created_at": self.created_at,
            "last_accessed": self.last_accessed,
            "access_count": self.access_count,
            "confidence": self.confidence,
        }


@dataclass
class PlanStep:
    """规划步骤"""

    step_id: str
    description: str
    action: str
    dependencies: list[str]
    estimated_time: float  # 秒
    completed: bool = False
    result: dict[str, Any] | None = None
    started_at: float | None = None
    completed_at: float | None = None


@dataclass
class SelfEvaluation:
    """自我评估结果"""

    timestamp: float = field(default_factory=time.time)
    metrics: dict[str, float] = field(default_factory=dict)
    improvement_suggestions: list[str] = field(default_factory=list)
    learned_patterns: list[str] = field(default_factory=list)


class AutonomousAgent:
    """
    自主 AI 代理

    功能：
    1. 自主任务分解与规划
    2. 长期记忆存储与检索
    3. 自我评估与改进
    4. 知识库构建与学习
    5. 多目标协同
    """

    def __init__(self, agent_id: str = "default"):
        self.agent_id = agent_id
        self.memory_store: dict[str, AgentMemory] = {}
        self.plans: dict[str, list[PlanStep]] = {}
        self.evaluation_history: list[SelfEvaluation] = []

        # 初始知识库
        self._initialize_knowledge_base()

    def _initialize_knowledge_base(self):
        """初始化知识库"""
        patterns = [
            "代码结构模式：MVC、分层架构、微服务",
            "设计模式：工厂、单例、观察者、策略",
            "代码最佳实践：命名规范、错误处理、日志记录",
            "性能优化：缓存、懒加载、并发控制",
            "安全实践：输入验证、权限控制、加密",
        ]

        for pattern in patterns:
            memory_id = hashlib.md5(pattern.encode()).hexdigest()
            self.memory_store[memory_id] = AgentMemory(
                memory_id=memory_id, content=pattern, tags=["pattern", "knowledge_base"]
            )

    def plan_goal(
        self, goal: AgentGoal, description: str, context: dict[str, Any]
    ) -> list[PlanStep]:
        """为目标制定规划"""
        plan_id = hashlib.md5(f"{goal.value}:{description}".encode()).hexdigest()

        if goal == AgentGoal.CODE_GENERATION:
            steps = self._plan_code_generation(description, context)
        elif goal == AgentGoal.PROBLEM_SOLVING:
            steps = self._plan_problem_solving(description, context)
        elif goal == AgentGoal.SYSTEM_DESIGN:
            steps = self._plan_system_design(description, context)
        elif goal == AgentGoal.CODE_REVIEW:
            steps = self._plan_code_review(description, context)
        elif goal == AgentGoal.LEARNING:
            steps = self._plan_learning(description, context)
        else:
            steps = self._plan_generic(description, context)

        self.plans[plan_id] = steps
        return steps

    def _plan_code_generation(self, description: str, context: dict[str, Any]) -> list[PlanStep]:
        """规划代码生成任务"""
        steps = []

        # 步骤1: 需求分析
        steps.append(
            PlanStep(
                step_id=f"step_1_{time.time()}",
                description="分析需求和要求",
                action="analyze_requirements",
                dependencies=[],
                estimated_time=30.0,
            )
        )

        # 步骤2: 设计架构
        steps.append(
            PlanStep(
                step_id=f"step_2_{time.time()}",
                description="设计代码架构",
                action="design_architecture",
                dependencies=["step_1"],
                estimated_time=60.0,
            )
        )

        # 步骤3: 生成代码
        steps.append(
            PlanStep(
                step_id=f"step_3_{time.time()}",
                description="生成实现代码",
                action="generate_code",
                dependencies=["step_2"],
                estimated_time=120.0,
            )
        )

        # 步骤4: 测试验证
        steps.append(
            PlanStep(
                step_id=f"step_4_{time.time()}",
                description="验证和测试代码",
                action="verify_test",
                dependencies=["step_3"],
                estimated_time=90.0,
            )
        )

        return steps

    def _plan_problem_solving(self, description: str, context: dict[str, Any]) -> list[PlanStep]:
        """规划问题解决任务"""
        steps = []

        # 步骤1: 问题定义
        steps.append(
            PlanStep(
                step_id=f"step_1_{time.time()}",
                description="明确定义问题",
                action="define_problem",
                dependencies=[],
                estimated_time=20.0,
            )
        )

        # 步骤2: 分析原因
        steps.append(
            PlanStep(
                step_id=f"step_2_{time.time()}",
                description="分析问题根本原因",
                action="analyze_root_cause",
                dependencies=["step_1"],
                estimated_time=40.0,
            )
        )

        # 步骤3: 方案设计
        steps.append(
            PlanStep(
                step_id=f"step_3_{time.time()}",
                description="设计解决方案",
                action="design_solution",
                dependencies=["step_2"],
                estimated_time=60.0,
            )
        )

        # 步骤4: 实施解决
        steps.append(
            PlanStep(
                step_id=f"step_4_{time.time()}",
                description="实施解决方案",
                action="implement_solution",
                dependencies=["step_3"],
                estimated_time=120.0,
            )
        )

        return steps

    def _plan_system_design(self, description: str, context: dict[str, Any]) -> list[PlanStep]:
        """规划系统设计任务"""
        steps = []

        steps.append(
            PlanStep(
                step_id=f"step_1_{time.time()}",
                description="分析系统需求",
                action="analyze_requirements",
                dependencies=[],
                estimated_time=30.0,
            )
        )

        steps.append(
            PlanStep(
                step_id=f"step_2_{time.time()}",
                description="设计系统架构",
                action="design_architecture",
                dependencies=["step_1"],
                estimated_time=60.0,
            )
        )

        steps.append(
            PlanStep(
                step_id=f"step_3_{time.time()}",
                description="设计数据模型",
                action="design_data_model",
                dependencies=["step_2"],
                estimated_time=45.0,
            )
        )

        steps.append(
            PlanStep(
                step_id=f"step_4_{time.time()}",
                description="定义接口与协议",
                action="define_interfaces",
                dependencies=["step_3"],
                estimated_time=45.0,
            )
        )

        steps.append(
            PlanStep(
                step_id=f"step_5_{time.time()}",
                description="编写实现方案文档",
                action="write_implementation_plan",
                dependencies=["step_4"],
                estimated_time=60.0,
            )
        )

        return steps

    def _plan_code_review(self, description: str, context: dict[str, Any]) -> list[PlanStep]:
        """规划代码审查任务"""
        steps = []

        steps.append(
            PlanStep(
                step_id=f"step_1_{time.time()}",
                description="定位待审查代码",
                action="locate_code",
                dependencies=[],
                estimated_time=20.0,
            )
        )

        steps.append(
            PlanStep(
                step_id=f"step_2_{time.time()}",
                description="静态审查代码质量",
                action="static_review",
                dependencies=["step_1"],
                estimated_time=40.0,
            )
        )

        steps.append(
            PlanStep(
                step_id=f"step_3_{time.time()}",
                description="识别潜在缺陷",
                action="identify_issues",
                dependencies=["step_2"],
                estimated_time=30.0,
            )
        )

        steps.append(
            PlanStep(
                step_id=f"step_4_{time.time()}",
                description="生成改进建议",
                action="suggest_improvements",
                dependencies=["step_3"],
                estimated_time=30.0,
            )
        )

        return steps

    def _plan_learning(self, description: str, context: dict[str, Any]) -> list[PlanStep]:
        """规划学习任务"""
        steps = []

        steps.append(
            PlanStep(
                step_id=f"step_1_{time.time()}",
                description="明确学习目标",
                action="define_objective",
                dependencies=[],
                estimated_time=15.0,
            )
        )

        steps.append(
            PlanStep(
                step_id=f"step_2_{time.time()}",
                description="收集学习资料",
                action="gather_materials",
                dependencies=["step_1"],
                estimated_time=40.0,
            )
        )

        steps.append(
            PlanStep(
                step_id=f"step_3_{time.time()}",
                description="学习与内化知识",
                action="study_and_internalize",
                dependencies=["step_2"],
                estimated_time=80.0,
            )
        )

        steps.append(
            PlanStep(
                step_id=f"step_4_{time.time()}",
                description="实践应用并检验",
                action="practice_and_validate",
                dependencies=["step_3"],
                estimated_time=60.0,
            )
        )

        return steps

    def _plan_generic(self, description: str, context: dict[str, Any]) -> list[PlanStep]:
        """通用规划"""
        return [
            PlanStep(
                step_id=f"step_1_{time.time()}",
                description="理解任务",
                action="understand_task",
                dependencies=[],
                estimated_time=30.0,
            ),
            PlanStep(
                step_id=f"step_2_{time.time()}",
                description="规划执行步骤",
                action="plan_execution",
                dependencies=["step_1"],
                estimated_time=45.0,
            ),
            PlanStep(
                step_id=f"step_3_{time.time()}",
                description="执行任务",
                action="execute_task",
                dependencies=["step_2"],
                estimated_time=180.0,
            ),
            PlanStep(
                step_id=f"step_4_{time.time()}",
                description="评估结果",
                action="evaluate_result",
                dependencies=["step_3"],
                estimated_time=30.0,
            ),
        ]

    def store_memory(self, content: str, tags: list[str]) -> str:
        """存储记忆"""
        memory_id = hashlib.md5(f"{content}:{time.time()}".encode()).hexdigest()
        memory = AgentMemory(memory_id=memory_id, content=content, tags=tags)
        self.memory_store[memory_id] = memory
        return memory_id

    def retrieve_memory(self, query: str, limit: int = 5) -> list[AgentMemory]:
        """检索记忆"""
        # 简单基于标签的检索
        query_words = query.lower().split()
        results = []

        for memory in self.memory_store.values():
            relevance_score = 0
            for word in query_words:
                if word in memory.content.lower():
                    relevance_score += 2
                for tag in memory.tags:
                    if word in tag.lower():
                        relevance_score += 1
            if relevance_score > 0:
                memory.access_count += 1
                memory.last_accessed = time.time()
                results.append((relevance_score, memory))

        results.sort(key=lambda x: x[0], reverse=True)
        return [memory for _, memory in results[:limit]]

    def execute_plan(self, plan_id: str) -> dict[str, Any]:
        """执行规划"""
        if plan_id not in self.plans:
            return {"status": "error", "message": "Plan not found"}

        steps = self.plans[plan_id]
        results = {}

        for i, step in enumerate(steps):
            step.started_at = time.time()

            # 执行步骤（这里模拟执行）
            print(f"[Agent] Executing step {i + 1}: {step.description}")

            # 实际执行逻辑
            if step.action == "analyze_requirements":
                result = self._analyze_requirements()
            elif step.action == "design_architecture":
                result = self._design_architecture()
            elif step.action == "generate_code":
                result = self._generate_code()
            elif step.action == "verify_test":
                result = self._verify_test()
            else:
                result = {"status": "completed", "output": f"Executed {step.action}"}

            step.completed = True
            step.result = result
            step.completed_at = time.time()

            results[step.step_id] = result

            # 存储执行结果到记忆
            self.store_memory(
                content=f"Executed {step.action}: {step.description}",
                tags=["execution", step.action],
            )

        return {"status": "success", "results": results}

    def self_evaluate(self) -> SelfEvaluation:
        """自我评估"""
        metrics = {
            "memory_size": len(self.memory_store),
            "completed_plans": sum(
                1 for plan in self.plans.values() if all(step.completed for step in plan)
            ),
            "total_steps": sum(len(plan) for plan in self.plans.values()),
            "average_execution_time": 0.0,  # 计算逻辑
        }

        # 分析记忆使用模式
        patterns = []
        memory_patterns = {}
        for memory in self.memory_store.values():
            for tag in memory.tags:
                memory_patterns[tag] = memory_patterns.get(tag, 0) + 1

        top_tags = sorted(memory_patterns.items(), key=lambda x: x[1], reverse=True)[:3]
        patterns = [f"频繁访问标签: {tag} ({count}次)" for tag, count in top_tags]

        # 改进建议
        suggestions = []
        if len(self.memory_store) < 100:
            suggestions.append("记忆库较小，建议积累更多知识")
        if len(self.evaluation_history) < 5:
            suggestions.append("评估历史较少，建议定期进行自我评估")

        evaluation = SelfEvaluation(
            metrics=metrics, improvement_suggestions=suggestions, learned_patterns=patterns
        )

        self.evaluation_history.append(evaluation)
        return evaluation

    def learn_from_experience(self, experience: dict[str, Any]) -> list[str]:
        """从经验中学习"""
        learned = []

        # 分析经验
        if experience.get("success"):
            # 成功经验
            pattern = f"成功模式: {experience.get('action', 'unknown')}"
            self.store_memory(pattern, ["success_pattern", "learning"])
            learned.append(pattern)
        elif experience.get("failure"):
            # 失败经验
            pattern = f"失败模式: {experience.get('action', 'unknown')} - {experience.get('reason', 'unknown')}"
            self.store_memory(pattern, ["failure_pattern", "learning"])
            learned.append(pattern)

        # 更新置信度
        for _memory_id, memory in self.memory_store.items():
            if "pattern" in memory.tags:
                # 增加相关模式的置信度
                memory.confidence = min(memory.confidence + 0.01, 1.0)

        return learned

    def _analyze_requirements(self) -> dict[str, Any]:
        """分析需求（示例实现）"""
        return {
            "status": "completed",
            "output": "Requirements analyzed",
            "requirements": ["功能完整性", "性能要求", "安全性考虑"],
        }

    def _design_architecture(self) -> dict[str, Any]:
        """设计架构（示例实现）"""
        return {
            "status": "completed",
            "output": "Architecture designed",
            "components": ["API 层", "业务逻辑层", "数据访问层"],
            "patterns": ["MVC", "Repository", "Factory"],
        }

    def _generate_code(self) -> dict[str, Any]:
        """生成代码（示例实现）"""
        return {
            "status": "completed",
            "output": "Code generated",
            "files": ["main.py", "config.py", "utils.py"],
            "lines_of_code": 150,
        }

    def _verify_test(self) -> dict[str, Any]:
        """验证测试（示例实现）"""
        return {
            "status": "completed",
            "output": "Tests verified",
            "tests_passed": 10,
            "coverage": 85.5,
        }

    def get_stats(self) -> dict[str, Any]:
        """获取代理统计"""
        return {
            "agent_id": self.agent_id,
            "memory_count": len(self.memory_store),
            "plan_count": len(self.plans),
            "evaluation_count": len(self.evaluation_history),
            "last_evaluation": self.evaluation_history[-1].timestamp
            if self.evaluation_history
            else None,
            "total_learned_patterns": sum(
                len(eval.learned_patterns) for eval in self.evaluation_history
            ),
        }


# 便捷函数
def create_autonomous_agent(agent_id: str = "default") -> AutonomousAgent:
    """创建自主 AI 代理"""
    return AutonomousAgent(agent_id)


if __name__ == "__main__":
    # 测试
    agent = create_autonomous_agent("test_agent")

    # 规划代码生成任务
    print("=== Planning Code Generation ===")
    plan = agent.plan_goal(
        AgentGoal.CODE_GENERATION,
        "Create a Flask REST API for user management",
        {"complexity": "medium", "priority": "high"},
    )
    print(f"Created plan with {len(plan)} steps")
    for i, step in enumerate(plan):
        print(f"  Step {i + 1}: {step.description}")

    # 存储记忆
    print("\n=== Storing Memory ===")
    memory_id = agent.store_memory(
        "Flask API 设计模式：使用蓝图进行模块化设计",
        ["flask", "api", "design_pattern", "best_practice"],
    )
    print(f"Stored memory: {memory_id}")

    # 检索记忆
    print("\n=== Retrieving Memory ===")
    memories = agent.retrieve_memory("flask api", limit=3)
    for memory in memories:
        print(f"  - {memory.content[:50]}...")

    # 执行规划
    print("\n=== Executing Plan ===")
    result = agent.execute_plan(next(iter(agent.plans.keys())))
    print(f"Execution result: {result['status']}")

    # 自我评估
    print("\n=== Self Evaluation ===")
    evaluation = agent.self_evaluate()
    print(f"Metrics: {evaluation.metrics}")
    print(f"Suggestions: {evaluation.improvement_suggestions}")

    # 从经验中学习
    print("\n=== Learning from Experience ===")
    experience = {
        "success": True,
        "action": "code_review",
        "result": "Improved code quality by 20%",
    }
    learned = agent.learn_from_experience(experience)
    print(f"Learned: {learned}")

    # 获取统计
    stats = agent.get_stats()
    print(f"\nAgent Stats: {stats}")

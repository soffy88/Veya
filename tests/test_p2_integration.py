"""
P2 能力集成测试
"""

import os

import pytest

from server.coordinator import Coordinator


@pytest.mark.asyncio
async def test_multimodal_integration():
    """测试多模态处理集成"""
    coordinator = Coordinator()

    # 测试图像处理（创建临时文件）
    test_image = "/tmp/test_image.png"
    with open(test_image, "w") as f:
        f.write("dummy image content")

    result = await coordinator.process_multimodal_input(test_image)
    assert result["status"] == "success"
    assert result.get("message") is not None
    print(f"Multimodal Image Result: {result}")

    # 清理
    if os.path.exists(test_image):
        os.remove(test_image)


@pytest.mark.asyncio
async def test_integration_hub():
    """测试生态集成中心"""
    from veya.integrations import create_integration_hub

    hub = create_integration_hub()

    # 列出集成
    integrations = hub.list_integrations()
    print(f"Registered integrations: {integrations}")

    # 如果配置了 Slack，测试发送消息
    if "slack" in integrations:
        result = await hub.send_to("slack", "success", {"message": "Test notification"})
        assert result.success or "Slack webhook URL not configured" in result.message
        print(f"Slack result: {result}")

    # 如果配置了 GitHub，测试创建 issue
    if "github" in integrations:
        result = await hub.send_to(
            "github",
            "create_issue",
            {
                "title": "Test issue from P2 integration",
                "body": "This is a test issue created during P2 integration testing.",
            },
        )
        assert result.success or "GitHub token not configured" in result.message
        print(f"GitHub result: {result}")


@pytest.mark.asyncio
async def test_collaboration_integration():
    """测试协作功能集成"""
    from veya.collaboration import create_collaboration_manager

    manager = create_collaboration_manager()

    # 创建会话
    session = await manager.create_session("user1", "P2 Collaboration Test")
    assert session.session_id
    print(f"Created session: {session.session_id}")

    # 用户加入
    await session.join("user2", "Alice", "write")
    await session.join("user3", "Bob", "read")

    # 发送消息
    success = await session.add_message("user2", "Hello from collaboration test!", "text")
    assert success

    # 获取会话信息
    info = session.get_info()
    assert info["collaborator_count"] >= 2
    print(f"Session info: {info}")

    # 创建版本
    version = await session.create_version("user1", "Initial state")
    assert version.version_id
    print(f"Created version: {version.version_id}")


@pytest.mark.asyncio
async def test_semantic_search_integration():
    """测试语义搜索集成"""
    from veya.semantic_search import create_semantic_search

    search = create_semantic_search()

    # 索引一些代码
    code = """
def load_model(name, version='latest'):
    \"\"\"Load a model by name and version.\"\"\"
    return {'name': name, 'version': version}

def save_model(model, path):
    \"\"\"Save model to disk.\"\"\"
    with open(path, 'w') as f:
        json.dump(model, f)

def train_model(data, epochs=10):
    \"\"\"Train a model.\"\"\"
    for epoch in range(epochs):
        print(f'Training epoch {epoch}')
    return model
"""

    search.index_file("test.py", code, chunk_size=5)

    # 搜索
    results = search.search("load model")
    assert len(results) > 0
    print(f"Search results: {len(results)}")
    for r in results[:2]:
        print(f"Score: {r.score}, File: {r.file_path}")
        print(f"Text: {r.text[:100]}...")

    # 推荐补全
    recommendations = search.recommend_completion("def load_")
    assert len(recommendations) > 0
    print(f"Code completion recommendations: {recommendations[:2]}")


@pytest.mark.asyncio
async def test_coordinator_p2_integration():
    """测试协调器 P2 集成"""
    coordinator = Coordinator()

    # 测试多模态处理
    if hasattr(coordinator, "multimodal_processor"):
        test_image = "/tmp/test_image.png"
        with open(test_image, "w") as f:
            f.write("dummy image content")

        result = await coordinator.process_multimodal_input(test_image)
        assert result["status"] == "success"

        # 清理
        if os.path.exists(test_image):
            os.remove(test_image)

    # 测试协作功能
    if hasattr(coordinator, "collaboration_manager"):
        session_resp = await coordinator.create_collaborative_session(
            "user1", "Coordinator P2 Test"
        )
        assert session_resp["status"] == "success"
        assert session_resp["session_id"]

        # 通过 manager 直接操作 session（协调器返回结构化 dict，不暴露 session 对象）
        from veya.collaboration import CollaborationManager

        manager: CollaborationManager = coordinator.collaboration_manager
        session = await manager.create_session("user1", "Coordinator P2 Test 2")

        # 加入用户
        await session.join("user2", "Alice", "write")

        # 发送消息
        success = await session.add_message("user2", "Hello from coordinator!", "text")
        assert success

    # 测试语义搜索
    if hasattr(coordinator, "semantic_search"):
        code = """
def load_model(name, version='latest'):
    \"\"\"Load a model by name and version.\"\"\"
    return {'name': name, 'version': version}
"""

        coordinator.semantic_search.index_file("test.py", code, chunk_size=5)
        results = coordinator.semantic_search.search("load model")
        assert len(results) > 0

        # 推荐补全
        recommendations = coordinator.semantic_search.recommend_completion("def load_")
        assert len(recommendations) > 0


if __name__ == "__main__":
    pytest.main([__file__, "-v"])

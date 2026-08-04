"""
语义搜索模块 - P2 核心能力
功能：基于 embedding 的代码语义搜索、相似度匹配、代码推荐
"""

from __future__ import annotations

import hashlib
import math
import re
from dataclasses import dataclass, field


@dataclass
class SearchResult:
    """搜索结果"""

    id: str
    text: str
    file_path: str
    score: float
    start_line: int
    end_line: int
    metadata: dict = field(default_factory=dict)


class EmbeddingModel:
    """
    Embedding 模型接口

    实际使用时应接入 sentence-transformers / OpenAI 等
    """

    def __init__(self, model_name: str = "default"):
        self.model_name = model_name

    def encode(self, texts: list[str]) -> list[list[float]]:
        """将文本编码为向量"""
        # 简化版：基于词频的向量
        return [self._simple_embedding(text) for text in texts]

    def _simple_embedding(self, text: str) -> list[float]:
        """简单的词频 embedding"""
        # 将文本转换为 128 维的词频向量
        vector = [0.0] * 128
        words = re.findall(r"\w+", text.lower())
        for word in words:
            idx = hash(word) % 128
            vector[idx] += 1.0

        # 归一化
        norm = math.sqrt(sum(x**2 for x in vector))
        if norm > 0:
            vector = [x / norm for x in vector]

        return vector


class SemanticSearch:
    """
    语义搜索引擎

    功能：
    1. 代码片段索引
    2. 基于 embedding 的相似度搜索
    3. 混合搜索（关键词 + 语义）
    4. 代码推荐
    """

    def __init__(self, embedding_model: EmbeddingModel | None = None):
        self.embedding_model = embedding_model or EmbeddingModel()
        self.documents: dict[str, dict] = {}  # id -> doc
        self.embeddings: dict[str, list[float]] = {}  # id -> vector
        self.keyword_index: dict[str, list[str]] = {}  # word -> doc_ids

    def _doc_id(self, file_path: str, start_line: int, end_line: int) -> str:
        """生成文档 ID"""
        key = f"{file_path}:{start_line}:{end_line}"
        return hashlib.md5(key.encode()).hexdigest()

    def _tokenize(self, text: str) -> list[str]:
        """分词"""
        return re.findall(r"\w+", text.lower())

    def index_file(self, file_path: str, content: str, chunk_size: int = 50) -> list[str]:
        """索引文件"""
        lines = content.split("\n")
        doc_ids = []

        for i in range(0, len(lines), chunk_size):
            start_line = i + 1
            end_line = min(i + chunk_size, len(lines))
            chunk = "\n".join(lines[i:end_line])

            if not chunk.strip():
                continue

            doc_id = self._doc_id(file_path, start_line, end_line)

            self.documents[doc_id] = {
                "id": doc_id,
                "file_path": file_path,
                "start_line": start_line,
                "end_line": end_line,
                "text": chunk,
            }

            # 构建关键词索引
            words = self._tokenize(chunk)
            for word in set(words):
                if word not in self.keyword_index:
                    self.keyword_index[word] = []
                if doc_id not in self.keyword_index[word]:
                    self.keyword_index[word].append(doc_id)

            doc_ids.append(doc_id)

        # 生成 embedding
        if doc_ids:
            chunks = [self.documents[doc_id]["text"] for doc_id in doc_ids]
            embeddings = self.embedding_model.encode(chunks)
            for doc_id, embedding in zip(doc_ids, embeddings, strict=False):
                self.embeddings[doc_id] = embedding

        return doc_ids

    def index_project(self, project_path: str, file_extensions: list[str] | None = None) -> int:
        """索引整个项目"""
        import os

        if file_extensions is None:
            file_extensions = [".py", ".js", ".ts", ".java", ".go", ".rs"]

        indexed = 0
        for root, _, files in os.walk(project_path):
            for file in files:
                if any(file.endswith(ext) for ext in file_extensions):
                    file_path = os.path.join(root, file)
                    try:
                        with open(file_path, encoding="utf-8") as f:
                            content = f.read()
                        self.index_file(file_path, content)
                        indexed += 1
                    except Exception as e:
                        print(f"[SemanticSearch] Failed to index {file_path}: {e}")

        return indexed

    def _cosine_similarity(self, a: list[float], b: list[float]) -> float:
        """计算余弦相似度"""
        dot = sum(x * y for x, y in zip(a, b, strict=False))
        norm_a = math.sqrt(sum(x**2 for x in a))
        norm_b = math.sqrt(sum(x**2 for x in b))
        if norm_a == 0 or norm_b == 0:
            return 0.0
        return dot / (norm_a * norm_b)

    def _keyword_search(self, query: str, top_k: int = 10) -> list[tuple[str, float]]:
        """关键词搜索"""
        words = self._tokenize(query)
        scores: dict[str, float] = {}

        for word in words:
            for doc_id in self.keyword_index.get(word, []):
                scores[doc_id] = scores.get(doc_id, 0) + 1.0

        # 归一化
        for doc_id in scores:
            doc_words = self._tokenize(self.documents[doc_id]["text"])
            scores[doc_id] /= math.sqrt(len(doc_words)) if doc_words else 1.0

        return sorted(scores.items(), key=lambda x: x[1], reverse=True)[:top_k]

    def _semantic_search(self, query: str, top_k: int = 10) -> list[tuple[str, float]]:
        """语义搜索"""
        query_embedding = self.embedding_model.encode([query])[0]

        scores = []
        for doc_id, embedding in self.embeddings.items():
            similarity = self._cosine_similarity(query_embedding, embedding)
            scores.append((doc_id, similarity))

        return sorted(scores, key=lambda x: x[1], reverse=True)[:top_k]

    def search(self, query: str, top_k: int = 10, hybrid: bool = True) -> list[SearchResult]:
        """
        搜索代码

        Args:
            query: 查询语句
            top_k: 返回结果数量
            hybrid: 是否混合关键词和语义搜索
        """
        if not self.documents:
            return []

        if hybrid:
            # 混合搜索
            keyword_results = {
                doc_id: score for doc_id, score in self._keyword_search(query, top_k * 2)
            }
            semantic_results = {
                doc_id: score for doc_id, score in self._semantic_search(query, top_k * 2)
            }

            # 合并并排序
            all_docs = set(keyword_results.keys()) | set(semantic_results.keys())
            combined = []
            for doc_id in all_docs:
                k_score = keyword_results.get(doc_id, 0)
                s_score = semantic_results.get(doc_id, 0)
                # 加权混合
                score = 0.3 * k_score + 0.7 * s_score
                combined.append((doc_id, score))

            results = sorted(combined, key=lambda x: x[1], reverse=True)[:top_k]
        else:
            results = self._semantic_search(query, top_k)

        return [
            SearchResult(
                id=doc_id,
                text=self.documents[doc_id]["text"],
                file_path=self.documents[doc_id]["file_path"],
                score=round(score, 4),
                start_line=self.documents[doc_id]["start_line"],
                end_line=self.documents[doc_id]["end_line"],
                metadata=self.documents[doc_id],
            )
            for doc_id, score in results
        ]

    def find_similar(self, text: str, top_k: int = 5) -> list[SearchResult]:
        """查找相似代码"""
        return self.search(text, top_k=top_k, hybrid=False)

    def recommend_completion(self, partial_code: str) -> list[str]:
        """推荐代码补全"""
        results = self.search(partial_code, top_k=3, hybrid=True)
        return [r.text for r in results]

    def get_stats(self) -> dict:
        """获取索引统计"""
        return {
            "indexed_documents": len(self.documents),
            "indexed_keywords": len(self.keyword_index),
            "model": self.embedding_model.model_name,
        }


# 便捷函数
def create_semantic_search(embedding_model: EmbeddingModel | None = None) -> SemanticSearch:
    """创建语义搜索引擎"""
    return SemanticSearch(embedding_model)


if __name__ == "__main__":
    # 测试
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

    # 测试搜索
    print("=== Searching for model loading ===")
    results = search.search("load model")
    for r in results:
        print(f"Score: {r.score}, File: {r.file_path}, Lines: {r.start_line}-{r.end_line}")
        print(f"Text: {r.text[:100]}...")
        print()

    # 测试推荐
    print("=== Code completion recommendation ===")
    recommendations = search.recommend_completion("def load_")
    for i, rec in enumerate(recommendations, 1):
        print(f"{i}. {rec[:100]}...")

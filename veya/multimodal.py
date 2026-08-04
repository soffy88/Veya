"""
多模态处理模块 - P2 核心能力
功能：图像理解、OCR、文档解析（PDF/Word/图片）
"""

from __future__ import annotations

import base64
import json
import os
import re
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, ClassVar


@dataclass
class MultimodalResult:
    """多模态处理结果"""

    source_type: str  # image, document, audio
    source_path: str
    text: str = ""
    description: str = ""
    extracted_code: str | None = None
    metadata: dict[str, Any] = field(default_factory=dict)
    success: bool = True
    error: str = ""


class ImageProcessor:
    """
    图像处理器

    功能：
    1. OCR 文本提取
    2. 代码截图识别
    3. 图像描述生成
    4. 图像编码（用于 LLM）
    """

    MEDIA_TYPES: ClassVar[dict[str, str]] = {
        "png": "image/png",
        "jpg": "image/jpeg",
        "jpeg": "image/jpeg",
        "gif": "image/gif",
        "webp": "image/webp",
        "bmp": "image/bmp",
    }

    def __init__(self):
        self.supported_formats = {".png", ".jpg", ".jpeg", ".gif", ".webp", ".bmp"}

    def _is_supported(self, path: str) -> bool:
        """检查图像格式是否支持"""
        return Path(path).suffix.lower() in self.supported_formats

    def encode_image(self, image_path: str) -> str | None:
        """将图像编码为 base64"""
        if not self._is_supported(image_path):
            return None
        try:
            with open(image_path, "rb") as f:
                return base64.b64encode(f.read()).decode("utf-8")
        except Exception as e:
            print(f"[ImageProcessor] Failed to encode image: {e}")
            return None

    def media_type(self, image_path: str) -> str:
        """根据扩展名返回 MIME 类型（默认 image/png）"""
        suffix = Path(image_path).suffix.lstrip(".").lower()
        return self.MEDIA_TYPES.get(suffix, "image/png")

    def to_content_block(self, image_path: str) -> dict[str, Any] | None:
        """将图像编码为 OpenAI 风格 image_url 内容块（G12，供 provider 消费）。

        返回 ``{"type": "image_url", "image_url": {"url": "data:<mime>;base64,..."}}``；
        文件不存在或不支持时返回 None。
        """
        b64 = self.encode_image(image_path)
        if b64 is None:
            return None
        return {
            "type": "image_url",
            "image_url": {
                "url": f"data:{self.media_type(image_path)};base64,{b64}",
            },
        }

    def extract_text_ocr(self, image_path: str) -> str:
        """OCR 提取文本（简化版，实际可接入 Tesseract/第三方 OCR）"""
        if not self._is_supported(image_path):
            return ""

        # 模拟 OCR 结果
        # 实际实现应使用 pytesseract 或其他 OCR 库
        return f"[OCR placeholder for {image_path}]"

    def is_code_screenshot(self, image_path: str) -> bool:
        """判断图像是否为代码截图"""
        text = self.extract_text_ocr(image_path)
        code_indicators = [
            "def ",
            "class ",
            "import ",
            "return ",
            "function",
            "const ",
            "let ",
            "var ",
            "=>",
            "#include",
        ]
        return any(indicator in text for indicator in code_indicators)

    def parse_code_from_image(self, image_path: str) -> str | None:
        """从代码截图中提取代码"""
        if not self.is_code_screenshot(image_path):
            return None

        # 简化实现：提取看起来是代码的部分
        text = self.extract_text_ocr(image_path)

        # 去除行号、提示符等
        lines = text.split("\n")
        cleaned_lines = []
        for line in lines:
            # 去除常见行号前缀
            cleaned = re.sub(r"^\s*\d+[:\.]?\s*", "", line)
            # 去除提示符
            cleaned = re.sub(r"^(?:>>>|\$|>)\s*", "", cleaned)
            if cleaned.strip():
                cleaned_lines.append(cleaned)

        return "\n".join(cleaned_lines)

    def analyze(self, image_path: str) -> MultimodalResult:
        """分析图像"""
        try:
            if not os.path.exists(image_path):
                return MultimodalResult(
                    source_type="image",
                    source_path=image_path,
                    success=False,
                    error="File not found",
                )

            base64_image = self.encode_image(image_path)
            text = self.extract_text_ocr(image_path)
            code = self.parse_code_from_image(image_path)

            # 生成描述
            description = f"Image: {Path(image_path).name}"
            if code:
                description += " (appears to be a code screenshot)"

            return MultimodalResult(
                source_type="image",
                source_path=image_path,
                text=text,
                description=description,
                extracted_code=code,
                metadata={
                    "base64_size": len(base64_image) if base64_image else 0,
                    "is_code_screenshot": code is not None,
                },
            )
        except Exception as e:
            return MultimodalResult(
                source_type="image", source_path=image_path, success=False, error=str(e)
            )


class DocumentProcessor:
    """
    文档处理器

    功能：
    1. PDF 文本提取
    2. Word 文档解析
    3. 文档分段
    4. 元数据提取
    """

    def __init__(self):
        self.supported_formats = {".pdf", ".docx", ".doc", ".txt", ".md"}

    def extract_pdf(self, doc_path: str) -> str:
        """提取 PDF 文本"""
        try:
            # 简化版：实际应使用 PyPDF2 / pdfplumber
            # 这里仅作为接口示例
            return f"[PDF text placeholder for {doc_path}]"
        except Exception as e:
            return f"[PDF extraction error: {e!s}]"

    def extract_docx(self, doc_path: str) -> str:
        """提取 Word 文档文本"""
        try:
            # 简化版：实际应使用 python-docx
            return f"[DOCX text placeholder for {doc_path}]"
        except Exception as e:
            return f"[DOCX extraction error: {e!s}]"

    def extract_text(self, doc_path: str) -> str:
        """通用文本提取"""
        suffix = Path(doc_path).suffix.lower()

        if suffix == ".pdf":
            return self.extract_pdf(doc_path)
        elif suffix in [".docx", ".doc"]:
            return self.extract_docx(doc_path)
        elif suffix in [".txt", ".md", ".py", ".js", ".json"]:
            with open(doc_path, encoding="utf-8") as f:
                return f.read()
        else:
            return f"[Unsupported document format: {suffix}]"

    def segment_document(
        self, doc_path: str, chunk_size: int = 1000, overlap: int = 100
    ) -> list[dict[str, Any]]:
        """将文档分段"""
        text = self.extract_text(doc_path)
        chunks = []

        start = 0
        while start < len(text):
            end = start + chunk_size
            chunk = text[start:end]
            chunks.append({"text": chunk, "start": start, "end": end, "source": doc_path})
            start = end - overlap

        return chunks

    def analyze(self, doc_path: str) -> MultimodalResult:
        """分析文档"""
        try:
            if not os.path.exists(doc_path):
                return MultimodalResult(
                    source_type="document",
                    source_path=doc_path,
                    success=False,
                    error="File not found",
                )

            text = self.extract_text(doc_path)
            segments = self.segment_document(doc_path)

            return MultimodalResult(
                source_type="document",
                source_path=doc_path,
                text=text,
                description=f"Document: {Path(doc_path).name}",
                metadata={
                    "format": Path(doc_path).suffix.lower(),
                    "length": len(text),
                    "segments": len(segments),
                },
            )
        except Exception as e:
            return MultimodalResult(
                source_type="document", source_path=doc_path, success=False, error=str(e)
            )


class MultimodalProcessor:
    """
    多模态处理器

    统一处理图像、文档、音频等多种输入
    """

    def __init__(self):
        self.image_processor = ImageProcessor()
        self.document_processor = DocumentProcessor()

    def process(self, file_path: str) -> MultimodalResult:
        """处理任意支持的文件"""
        suffix = Path(file_path).suffix.lower()

        if suffix in self.image_processor.supported_formats:
            return self.image_processor.analyze(file_path)
        elif suffix in self.document_processor.supported_formats:
            return self.document_processor.analyze(file_path)
        else:
            return MultimodalResult(
                source_type="unknown",
                source_path=file_path,
                success=False,
                error=f"Unsupported file format: {suffix}",
            )

    def process_batch(self, file_paths: list[str]) -> list[MultimodalResult]:
        """批量处理文件"""
        return [self.process(path) for path in file_paths]

    def prepare_for_llm(self, file_path: str) -> dict[str, Any] | None:
        """准备文件供 LLM 使用"""
        result = self.process(file_path)

        if not result.success:
            return None

        if result.source_type == "image":
            base64_image = self.image_processor.encode_image(file_path)
            return {
                "type": "image",
                "url": f"data:{self.image_processor.media_type(file_path)};base64,{base64_image}",
                "text": result.text,
                "description": result.description,
            }
        else:
            return {"type": "document", "text": result.text, "description": result.description}

    def build_vision_messages(
        self,
        text: str,
        image_paths: list[str],
        *,
        system: str | None = None,
    ) -> list[dict[str, Any]]:
        """构建可直接发给 LLM provider 的视觉消息（G12）。

        文本 + 图片 → OpenAI 风格 content blocks
        （``text`` 块 + ``image_url`` data-URI 块）；缺失/不支持的图片自动跳过。
        ``system`` 非空时置于首位。
        """
        blocks: list[dict[str, Any]] = [{"type": "text", "text": text}]
        for path in image_paths:
            block = self.image_processor.to_content_block(path)
            if block is not None:
                blocks.append(block)

        messages: list[dict[str, Any]] = [{"role": "user", "content": blocks}]
        if system:
            messages.insert(0, {"role": "system", "content": system})
        return messages


# 便捷函数
def create_multimodal_processor() -> MultimodalProcessor:
    """创建多模态处理器"""
    return MultimodalProcessor()


if __name__ == "__main__":
    # 测试
    processor = create_multimodal_processor()

    # 测试图像处理
    print("=== Testing Image Processing ===")
    # 创建一个临时测试图像文件
    test_image = "/tmp/test_image.txt"
    with open(test_image, "w") as f:
        f.write("def hello():\n    return 'world'")

    # 注意：这里用 .txt 模拟，实际应为图片
    result = processor.process(test_image)
    print(f"Result: {json.dumps(result.__dict__, default=str, indent=2)}")

    # 测试文档处理
    print("\n=== Testing Document Processing ===")
    test_doc = "/tmp/test_doc.md"
    with open(test_doc, "w") as f:
        f.write("# Hello\n\nThis is a test document.\n\n## Section 2\n\nMore content here." * 10)

    result = processor.process(test_doc)
    print(f"Result: {json.dumps(result.__dict__, default=str, indent=2)}")
    print(f"Segments: {len(processor.document_processor.segment_document(test_doc))}")

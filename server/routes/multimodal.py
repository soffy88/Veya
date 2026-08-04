"""
多模态处理 API - P2 核心能力
提供图像、文档等多模态内容的处理能力
"""

from __future__ import annotations

from typing import Any

from fastapi import APIRouter, HTTPException
from pydantic import BaseModel

from veya.multimodal import create_multimodal_processor

router = APIRouter(prefix="/multimodal", tags=["multimodal"])

# 全局多模态处理器
processor = create_multimodal_processor()


class MultimodalRequest(BaseModel):
    """多模态请求"""

    file_path: str


class ImageAnalysisRequest(BaseModel):
    """图像分析请求"""

    image_path: str


class DocumentAnalysisRequest(BaseModel):
    """文档分析请求"""

    document_path: str


@router.post("/analyze")
async def analyze_file(request: MultimodalRequest) -> dict[str, Any]:
    """分析文件（图像或文档）"""
    try:
        result = processor.process(request.file_path)

        if not result.success:
            raise HTTPException(status_code=500, detail=f"Analysis failed: {result.error}")

        return {"status": "success", "result": result.__dict__}
    except Exception as e:
        raise HTTPException(status_code=500, detail=f"Failed to analyze file: {e!s}")


@router.post("/image/analyze")
async def analyze_image(request: ImageAnalysisRequest) -> dict[str, Any]:
    """分析图像"""
    try:
        result = processor.image_processor.analyze(request.image_path)

        if not result.success:
            raise HTTPException(status_code=500, detail=f"Image analysis failed: {result.error}")

        return {"status": "success", "result": result.__dict__}
    except Exception as e:
        raise HTTPException(status_code=500, detail=f"Failed to analyze image: {e!s}")


@router.post("/document/analyze")
async def analyze_document(request: DocumentAnalysisRequest) -> dict[str, Any]:
    """分析文档"""
    try:
        result = processor.document_processor.analyze(request.document_path)

        if not result.success:
            raise HTTPException(status_code=500, detail=f"Document analysis failed: {result.error}")

        return {"status": "success", "result": result.__dict__}
    except Exception as e:
        raise HTTPException(status_code=500, detail=f"Failed to analyze document: {e!s}")


@router.post("/image/ocr")
async def extract_ocr(request: ImageAnalysisRequest) -> dict[str, Any]:
    """从图像中提取文本（OCR）"""
    try:
        image_processor = processor.image_processor
        text = image_processor.extract_text_ocr(request.image_path)

        return {"status": "success", "text": text}
    except Exception as e:
        raise HTTPException(status_code=500, detail=f"OCR extraction failed: {e!s}")


@router.post("/image/code")
async def extract_code_from_image(request: ImageAnalysisRequest) -> dict[str, Any]:
    """从代码截图中提取代码"""
    try:
        image_processor = processor.image_processor
        code = image_processor.parse_code_from_image(request.image_path)

        if code is None:
            return {"status": "success", "code": "", "message": "No code detected in image"}

        return {"status": "success", "code": code}
    except Exception as e:
        raise HTTPException(status_code=500, detail=f"Code extraction failed: {e!s}")


@router.post("/document/text")
async def extract_text_from_document(request: DocumentAnalysisRequest) -> dict[str, Any]:
    """从文档中提取文本"""
    try:
        document_processor = processor.document_processor
        text = document_processor.extract_text(request.document_path)

        return {"status": "success", "text": text}
    except Exception as e:
        raise HTTPException(status_code=500, detail=f"Text extraction failed: {e!s}")


@router.post("/document/segments")
async def segment_document(request: DocumentAnalysisRequest) -> dict[str, Any]:
    """将文档分段"""
    try:
        document_processor = processor.document_processor
        segments = document_processor.segment_document(request.document_path)

        return {"status": "success", "segments": segments}
    except Exception as e:
        raise HTTPException(status_code=500, detail=f"Document segmentation failed: {e!s}")


@router.post("/prepare-for-llm")
async def prepare_for_llm(request: MultimodalRequest) -> dict[str, Any]:
    """准备文件供 LLM 使用"""
    try:
        prepared = processor.prepare_for_llm(request.file_path)

        if prepared is None:
            raise HTTPException(status_code=500, detail="Failed to prepare file for LLM")

        return {"status": "success", "prepared": prepared}
    except Exception as e:
        raise HTTPException(status_code=500, detail=f"Failed to prepare file: {e!s}")

"""
跨语言支持 API - P3 核心能力
提供多语言代码理解、翻译、分析等功能
"""

from __future__ import annotations

from typing import Any

from fastapi import APIRouter, HTTPException
from pydantic import BaseModel

from hicode.cross_language import create_cross_language_translator

router = APIRouter(prefix="/cross-language", tags=["cross-language"])

# 全局跨语言翻译器
translator = create_cross_language_translator()


class TranslateRequest(BaseModel):
    source_code: str
    source_language: str  # python, java, cpp, rust, javascript, typescript, go
    target_language: str


class AnalyzeProjectRequest(BaseModel):
    project_path: str
    extensions: list[str] | None = None


@router.post("/translate")
async def translate_code(request: TranslateRequest) -> dict[str, Any]:
    """翻译代码"""
    try:
        from hicode.cross_language import Language

        # 转换语言字符串为枚举
        source_lang = getattr(Language, request.source_language.upper(), None)
        target_lang = getattr(Language, request.target_language.upper(), None)

        if not source_lang:
            raise HTTPException(
                status_code=400, detail=f"Invalid source language: {request.source_language}"
            )
        if not target_lang:
            raise HTTPException(
                status_code=400, detail=f"Invalid target language: {request.target_language}"
            )

        result = translator.translate(request.source_code, source_lang, target_lang)

        return {"status": "success", "translation": result.__dict__}
    except Exception as e:
        raise HTTPException(status_code=500, detail=f"Translation failed: {e!s}")


@router.post("/analyze-project")
async def analyze_project(request: AnalyzeProjectRequest) -> dict[str, Any]:
    """分析项目中的多语言文件"""
    try:
        stats = translator.analyze_project(request.project_path)
        return {"status": "success", "language_stats": stats}
    except Exception as e:
        raise HTTPException(status_code=500, detail=f"Project analysis failed: {e!s}")


@router.post("/parse")
async def parse_file(file_path: str, language: str) -> dict[str, Any]:
    """解析文件"""
    try:
        from hicode.cross_language import Language

        lang = getattr(Language, language.upper(), None)
        if not lang:
            raise HTTPException(status_code=400, detail=f"Invalid language: {language}")

        # 根据语言选择解析器
        if lang == Language.PYTHON:
            from hicode.cross_language import PythonParser

            parser = PythonParser()
        elif lang == Language.JAVA:
            from hicode.cross_language import JavaParser

            parser = JavaParser()
        else:
            return {"status": "failed", "message": f"Parser not available for {language}"}

        parsed_data = parser.parse_file(file_path)
        return {"status": "success", "parsed_data": parsed_data}
    except Exception as e:
        raise HTTPException(status_code=500, detail=f"File parsing failed: {e!s}")


@router.get("/languages")
async def list_supported_languages() -> dict[str, Any]:
    """列出支持的语言"""
    try:
        from hicode.cross_language import Language

        languages = [
            {
                "name": lang.value,
                "display_name": lang.name.title(),
                "extensions": [f".{lang.value}"],  # 简化
            }
            for lang in Language
        ]

        return {"status": "success", "languages": languages}
    except Exception as e:
        raise HTTPException(status_code=500, detail=f"Failed to list languages: {e!s}")

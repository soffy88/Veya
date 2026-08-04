# veya.multimodal — 多模态（G12）

图像编码为 OpenAI 风格 content blocks（`image_url` data-URI），
构建可直接发送给 provider 的视觉消息。

::: veya.multimodal
    options:
      filters: ["!^_"]
      members:
        - MultimodalResult
        - ImageProcessor
        - DocumentProcessor
        - MultimodalProcessor
        - create_multimodal_processor

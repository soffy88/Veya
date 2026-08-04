# veya.llm — LLM Provider 层（G1）

统一 OpenAI / Anthropic / DashScope provider 的补全、流式、工具调用与成本计算；
无 key 时自动 stub 降级。

::: veya.llm
    options:
      filters: ["!^_"]
      members:
        - get_provider_config
        - get_api_key
        - calc_cost
        - provider_call
        - provider_stream
        - prepare_messages_for_provider
        - llm_call
        - llm_stream

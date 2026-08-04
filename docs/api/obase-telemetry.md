# veya.obase.telemetry — 3O 遥测（§7）

JSONL trace + ContextVar 传播通道（C1 铁律）+ `@traced` 装饰器。

::: veya.obase.telemetry
    options:
      filters: ["!^_"]
      members:
        - TraceContext
        - begin_trace
        - end_trace
        - emit
        - set_emitter
        - current_trace
        - traced
        - jsonl_write
        - latest_trace

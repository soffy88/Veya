# veya.sandbox — 沙箱执行（G4）

子进程隔离：`ulimit -v`（内存）/ `ulimit -t`（CPU）仅在子进程生效，
危险命令前置拦截，参数数组执行（无 shell 解析注入面）。

::: veya.sandbox
    options:
      filters: ["!^_"]
      members:
        - Sandbox
        - execute_args
        - run_script
        - is_dangerous_command
        - is_dangerous_argv
        - MAX_MEMORY_MB
        - CPU_TIMEOUT_SECONDS

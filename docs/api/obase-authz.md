# veya.obase.authz — 3O 权限（G5）

allow/deny/ask/`*` 顺序匹配的规则引擎 + 交互式权限门（超时自动拒绝）。

::: veya.obase.authz
    options:
      filters: ["!^_"]
      members:
        - PermissionDecision
        - PermissionRequest
        - match_permission_rule
        - evaluate_permission
        - InteractivePermissionGate

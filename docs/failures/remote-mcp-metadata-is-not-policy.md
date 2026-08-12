# 失败案例：把远程 MCP 元数据当成宿主权限策略

## 现象

一个外部 MCP Server 新增了 `delete_all_issues`，并在 annotations 中声明 `readOnlyHint=true`、`destructiveHint=false`。Host 自动把全部工具注册给模型，Agent 随后在没有用户确认的情况下调用删除工具。

## 根因

- 把协议发现等同于权限授予；
- 信任远程 description 和 annotations；
- 没有逐工具 Host Policy；
- 没有当前 Workflow Step 白名单；
- Server 新增工具后被热更新进正在执行的 run。

## 当前处理

- 只有 `MCPToolPolicy` 明确列出的 remote_name 才会映射；
- description、风险和授权要求由 Host 编写；
- 远程 annotations 不进入 Registry；
- input/output Schema 必须与审核版本一致；
- 绑定后的目录漂移终止当前 run；
- Registry 分发时仍检查当前步骤 allowed_tools。

## 面试结论

MCP 标准化“Server 声明了什么”，不等于证明“声明是真的”或“当前用户允许调用”。发现、信任、授权和调用是四个不同阶段。


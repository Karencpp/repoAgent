# 失败案例：把 MCP 工具业务失败当成协议断线

## 现象

远程工具返回合法 `CallToolResult`，其中 `isError=true`，内容说明“当前用户无权读取该工单”。Client 把它统一包装成连接异常并自动重试，重复调用仍然失败，还掩盖了真正的权限原因。

另一次 HTTP 返回 502，系统却把它当成工具业务错误交给模型，模型错误地推断目标工单不存在。

## 根因

- 没有区分传输、JSON-RPC 和 Tool Result 三层；
- 认为“RPC 有响应”等于“工具成功”；
- 认为所有失败都可以重试；
- 错误类型只保存自由文本。

## 当前处理

```text
HTTP/JSON-RPC/解析错误 → protocol/infrastructure error
Timeout             → TIMEOUT，retryable
CallToolResult.isError → EXECUTION_ERROR
Invalid local args  → INVALID_ARGUMENT，不发送请求
InputRequired       → INPUT_REQUIRED，等待 Host
```

## 面试结论

MCP 调用至少有两层成功语义：协议是否成功完成一次请求，以及远程工具是否完成业务目标。重试和反思策略必须依据结构化错误层级。


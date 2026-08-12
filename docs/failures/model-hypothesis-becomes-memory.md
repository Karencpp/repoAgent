# 失败案例：模型假设被固化为长期事实

## 现象

一次诊断中，模型猜测“支付重复扣款可能由幂等记录写入过晚导致”。系统把这句总结直接保存为长期事实。后续每次检索都把它注入上下文，Agent 越来越确信这个原因，即使测试已经证明问题来自重试配置。

## 根因

- Memory 没有 claim_status；
- 写入时没有 Evidence；
- 模型可以直接创建 verified 记录；
- refuted 和 superseded 内容仍参与检索；
- 相似度排名被误当成真实性排名。

## 当前处理

- 所有记忆区分 hypothesis、verified 和 refuted；
- verified 必须有 Evidence；
- 正常 Agent 只开放只读 Memory Tool；
- Workflow Manager 只记录结构化运行事件和评估引用；
- 默认检索只返回 active verified；
- 新事实通过 supersede 原子替代旧事实；
- 版本级记忆默认只对相同 repo revision 有效。

## 面试结论

长期记忆会放大历史错误。Memory write 比 Memory search 风险更高，必须保留事实状态、证据和审核边界，不能把模型自信程度当作真值。


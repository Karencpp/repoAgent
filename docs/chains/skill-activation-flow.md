# Skill v2 发现、激活与执行链路

## 能力包结构

```text
skill-name/
├─ SKILL.md                 # 给模型的入口规程
├─ skill.yaml               # 版本、路由、工具、资源、依赖和脚本清单
├─ references/              # 激活时加载的详细知识
├─ scripts/                 # 确定性操作
├─ schemas/                 # 脚本输入输出契约
├─ assets/                  # 报告模板等静态资源
├─ tests/                   # 能力包自测数据
└─ agents/openai.yaml       # 客户端展示元数据
```

`SKILL.md` 保持开放格式的最小 frontmatter；`skill.yaml` 是 RepoAgent 的运行时扩展。任务进度不写回 Skill，而是进入 Graph State 和 Checkpoint。

## 发现与激活

```text
显式 trusted_roots
  → 读取 SKILL.md frontmatter + skill.yaml
  → 校验 SemVer、路径、Schema 声明、依赖和符号链接
  → SkillDescriptor（不含正文和参考资料）
  → Router 根据 goal、mode、trigger、tag 打分
  → 读取选中 Skill 的正文与 instruction_resources
  → 计算整个能力包 SHA-256
  → ActivatedSkill
  → Trusted Instructions
```

发现阶段只保留轻量目录信息；脚本、Schema、资产和测试不会进入模型上下文。激活时只自动装载声明为 `instruction_resources` 的参考资料。

## 工具与脚本权限

```text
已注册工具 ∩ 当前 Step 白名单 ∩ Skill allowed_tools
                         ↓
                   effective_tools
```

`required_tools` 只检查宿主运行环境是否具备能力，不会自动给当前步骤授权。脚本会注册成普通 Tool，和其他工具一样经过模型可见性过滤、参数 Schema 校验和 Registry 分发。

```text
ReAct tool_call
  → 当前 Skill 名称作用域校验
  → 输入 JSON Schema 校验
  → 隔离子进程：shell=False、超时、输出上限、最小环境变量、无 API Key
  → JSON stdin/stdout
  → 输出 JSON Schema 校验
  → ToolResult / Observation
```

这不是完整操作系统沙箱，所以只允许宿主已审核、安装在可信根的脚本；目标仓库中的 Skill 或脚本不会自动执行。

## Checkpoint 恢复

每条 `StepExecution` 保存 Skill 的 name、version、能力包 hash 和路由原因。后续步骤或恢复执行时，若同名 Skill 的版本或整个包内容发生变化，任务失败并要求重新开始，避免一次任务混用两版规程。


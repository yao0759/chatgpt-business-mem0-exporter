# ChatGPT Business 对话导出与 mem0 导入

这个项目通过 Playwright 连接到已经登录 ChatGPT Business 的 Microsoft Edge，先扫描侧栏中的对话链接，再将对话保存为 JSON 和 Markdown。随后可以把原始消息按 `user` / `assistant` 角色写入自托管 mem0，也可以调用任何兼容 OpenAI Chat Completions API 的第三方模型或 Agent 服务，将对话提炼成带证据的长期记忆后再写入 mem0。Ark 是默认配置示例，不是必需依赖。

ChatGPT Business 当前没有面向成员的自助数据导出功能，因此本项目读取浏览器已经渲染、且当前账号有权访问的内容。它不绕过登录、工作区权限或保留策略。

## 功能

- 连接当前已登录的 Edge，不保存账号密码。
- 先扫描全部侧栏对话链接，生成可复用的 `conversation_manifest.json`。
- 按链接清单增量导出，已有内容未变化时跳过。
- 同时保存 JSON 和 Markdown，并校验消息数及代码块数量。
- 等待页面内容连续稳定后才保存，避免把尚未加载完整的页面当作成功。
- 遇到 HTTP 403、429 或页面访问限制时停止批处理并保留进度。
- 原文导入时保留 `user`、`assistant`、`system`、`tool` 角色及消息顺序。
- 可用兼容 OpenAI Chat Completions API 的第三方模型或 Agent 服务，提炼已验证方案、待验证建议、失败尝试、用户偏好、项目背景和未解决问题。
- 使用本地检查点与 mem0 元数据去重，支持中断后继续。

## 安全边界

仓库不包含任何对话、导出文件、日志、检查点、审核结果或密钥。`.gitignore` 会排除常见导出和运行目录，但首次推送前仍应运行 `git status` 检查。

密钥只从环境变量读取：

- `MEM0_API_KEY`：mem0 服务密钥。
- `MEM0_BASE_URL`：mem0 地址，默认 `http://127.0.0.1:18765`。
- `MEM0_USER_ID`：写入 mem0 时使用的用户标识，默认 `default`。
- `ARK_API_KEY`：提炼服务的 API 密钥。变量名为兼容旧版本而保留，可填写任意兼容服务的密钥。

提炼脚本会遮蔽常见格式的密钥和令牌，但自动遮蔽不能保证识别所有敏感信息。若对话包含机密资料，先审核本地 JSONL，再执行写入。

## 环境要求

- Windows 10/11
- Python 3.10 或更高版本
- Microsoft Edge
- 可以访问 ChatGPT Business 的账号
- 自托管 mem0 兼容服务
- 可选：提供 OpenAI API 兼容 `/chat/completions` 接口的第三方模型或 Agent 服务

安装依赖：

```powershell
git clone https://github.com/yao0759/chatgpt-business-mem0-exporter.git
cd chatgpt-business-mem0-exporter
python -m venv .venv
.\.venv\Scripts\Activate.ps1
python -m pip install -r requirements.txt
python -m playwright install msedge
```

## 1. 开启 Edge 远程调试

1. 保持日常使用的 Edge 打开，并登录正确的 ChatGPT Business 工作区。
2. 在地址栏打开 `edge://inspect`。
3. 开启 **Allow remote debugging for this browser instance**。
4. 保持至少一个 `chatgpt.com` 标签页打开。

脚本读取 Edge 用户数据目录中的 `DevToolsActivePort`，连接当前浏览器实例。它不会启动新的浏览器配置，也不会要求在脚本中输入账号密码。

## 2. 只扫描对话链接

推荐先完成链接发现，再导出正文：

```powershell
python .\scripts\export_chatgpt_business_incremental.py `
  --scan-links-only `
  --max-scan-rounds 600 `
  --output .\export
```

结果写入 `export\conversation_manifest.json`。只有 `scan_complete` 为 `true` 时，后续批量导出才会接受这份清单。

侧栏采用延迟加载。扫描器会反复滚动，并要求在列表底部连续多轮保持链接数量与滚动高度不变，才判定扫描完成。这个数量代表侧栏能发现的对话，不代表归档、项目内部或已被工作区策略隐藏的所有内容。

## 3. 按清单导出正文

```powershell
python .\scripts\export_chatgpt_business_incremental.py `
  --manifest .\export\conversation_manifest.json `
  --output .\export `
  --cooldown 20 `
  --retries 3
```

输出结构：

```text
export/
├── conversation_manifest.json
├── export_status.json
├── errors.jsonl
├── json/
│   └── <conversation-id>.json
└── markdown/
    └── <conversation-id>.md
```

`export_status.json` 的常见状态：

- `running`：正在处理。
- `completed`：清单全部成功。
- `completed_with_errors`：完成遍历，但存在失败项。
- `paused_rate_limit`：检测到访问限制并停止。

建议 `--cooldown` 不低于 15 秒。大量对话可增加到 30–60 秒。不要同时运行多个正文导出进程。

也可以只导出单个或少量链接：

```powershell
python .\scripts\export_chatgpt_business_incremental.py `
  --url "https://chatgpt.com/c/<conversation-id>" `
  --output .\export
```

多条链接可以重复使用 `--url`，或用 `--url-file links.txt`，每行一个链接。

## 4. 将原始对话导入 mem0

先设置当前 PowerShell 会话中的环境变量：

```powershell
$env:MEM0_BASE_URL = "http://127.0.0.1:18765"
$env:MEM0_API_KEY = "你的密钥"
```

先预览统计，不写入：

```powershell
python .\scripts\import_chatgpt_business_json_to_mem0.py `
  .\export\json `
  --checkpoint .\state\raw-import.jsonl
```

确认后执行：

```powershell
python .\scripts\import_chatgpt_business_json_to_mem0.py `
  .\export\json `
  --checkpoint .\state\raw-import.jsonl `
  --execute
```

该导入器直接读取 JSON 的角色字段。每条 mem0 记录都会包含：

- `role`：`user` 或 `assistant` 等原始角色；
- `message_index`：消息在对话中的顺序；
- `conversation_id`、标题和原始 URL；
- 内容哈希、分片序号和总片数。

写入使用 `infer=false`，避免 mem0 再次改写原文。长消息才会拆分，每个分片仍保留角色和顺序。

## 5. 使用兼容 OpenAI API 的第三方服务提炼长期记忆

提炼器不限定服务商。第三方服务需要满足以下条件：

- 提供兼容 OpenAI Chat Completions 的 HTTP 接口；
- 接受 `model`、`messages`、`temperature` 和 `max_tokens` 字段；
- 返回 `choices[0].message.content`；
- 能按提示输出 JSON。

设置第三方服务密钥。`ARK_API_KEY` 是历史兼容变量名，并不表示只能使用 Ark：

```powershell
$env:ARK_API_KEY = "第三方服务的 API 密钥"
```

生成本地审核文件：

```powershell
New-Item -ItemType Directory -Force .\state | Out-Null
python .\scripts\distill_chatgpt_business_to_mem0.py prepare `
  .\export\json `
  --output .\state\distilled-review.jsonl `
  --ark-url "https://your-provider.example/v1" `
  --model "your-model-name" `
  --max-chars 5000
```

`--ark-url` 同样是为兼容旧版本保留的参数名。这里应填写服务的 API 根地址，脚本会在其后调用 `/chat/completions`。例如，Ark 可以继续使用默认地址和默认模型；其他兼容服务只需替换 URL、模型名和密钥。

脚本要求每条提炼结果包含原消息中的逐字证据，并验证证据确实存在。助手提出的做法在没有用户确认时只能标记为“待验证建议”。

预览待写入内容：

```powershell
python .\scripts\distill_chatgpt_business_to_mem0.py import `
  .\state\distilled-review.jsonl `
  --checkpoint .\state\distilled-import.jsonl
```

审核后写入：

```powershell
python .\scripts\distill_chatgpt_business_to_mem0.py import `
  .\state\distilled-review.jsonl `
  --checkpoint .\state\distilled-import.jsonl `
  --agent-id chatgpt-business-distilled `
  --execute
```

## 6. 后台与自动衔接

仓库保留了三个编排脚本：

- `run_chatgpt_mem0_background.py`：依次执行原文导入、第三方模型提炼、重试和提炼记忆导入。
- `import_distilled_incrementally.py`：提炼进行时周期性写入已经准备好的记忆。
- `run_current_workspace_mem0_import.py`：等待旧任务与新导出都完成，并检查 JSON、Markdown 和清单数量一致后再导入。

这些脚本把运行状态写入 `state/`。导出目录可通过环境变量指定：

```powershell
$env:CHATGPT_EXPORT_DIR = (Resolve-Path .\export).Path
$env:CHATGPT_CURRENT_EXPORT_DIR = (Resolve-Path .\export).Path
```

自动衔接脚本还要求对应的状态文件存在，适合已经采用这套流水线的环境。首次使用建议先按第 2–5 节手动执行，确认服务地址、权限和提炼质量后再启用编排。

## 旧版完整导出器

`export_chatgpt_business_edge.py` 是功能更完整的底层导出器，支持 HTML、MHTML、生成文件下载、离线重建和旧记录修复。日常增量备份建议使用 `export_chatgpt_business_incremental.py`，它对访问节奏和完整性检查更严格。

查看完整参数：

```powershell
python .\scripts\export_chatgpt_business_edge.py --help
python .\scripts\export_chatgpt_business_incremental.py --help
python .\scripts\import_chatgpt_business_json_to_mem0.py --help
python .\scripts\distill_chatgpt_business_to_mem0.py --help
```

## 故障排查

### 无法连接 Edge CDP

重新打开 `edge://inspect`，关闭后再开启远程调试，并确认 `chatgpt.com` 标签页仍在同一个 Edge 实例中。

### Markdown 比页面短

以 JSON 为权威数据源。增量导出器会将单独提取的代码块重新嵌入 Markdown，并核对消息标题数和代码围栏数；校验失败时不会覆盖正式文件。

### 对话数量比预期少

重新运行链接扫描。侧栏之外的归档对话、项目对话或工作区不可见内容不会自动出现在清单中。

### 出现访问频率限制

停止当前批次，等待限制解除后沿用同一清单和输出目录继续。提高 `--cooldown`，避免并行导出。

### mem0 重复记录

保留并复用同一个 checkpoint 文件。导入器也会查询 mem0 中的 `source_key` 元数据，但本地检查点仍是最快且最明确的续传依据。

## 文件说明

```text
scripts/
├── export_chatgpt_business_edge.py
├── export_chatgpt_business_incremental.py
├── import_chatgpt_business_to_mem0.py
├── import_chatgpt_business_json_to_mem0.py
├── distill_chatgpt_business_to_mem0.py
├── run_chatgpt_mem0_background.py
├── import_distilled_incrementally.py
└── run_current_workspace_mem0_import.py
```

本项目依赖 ChatGPT 网页结构，网页更新后选择器可能需要同步调整。开始大批量操作前，建议先用一条对话验证导出、Markdown、角色和 mem0 检索结果。

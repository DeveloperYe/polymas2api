# polymas2api

把 polymas（智慧树 / Polymas）教学平台的 AI 助教包装成一个 **OpenAI 兼容的本地 API**，并附带一个可运维的控制台面板。

> 协议与平台前端完全一致（`POST /ai-agent/assistant/v1/chat/stream`，SSE 事件
> `REASONING` / `ANSWER` / `ALL_FINISHED`）。**账号与它对应的助手都存在
> `accounts.json` 里，从面板添加即可**，不需要手改配置文件。

```
POST http://127.0.0.1:8787/v1/chat/completions
```

纯标准库实现，零运行时依赖。

---

## 快速开始

```bash
cd polymas2api

# 1. 生成配置模板（只有设置项，没有任何密钥）
python -m polymas2api.cli --print-example config.json

# 2. 启动
python -m polymas2api.cli -c config.json
```

启动后打开 `http://127.0.0.1:8787/panel/`，点右上角 **「＋ 添加账号」**，把
浏览器里那串 `ai-poly` cookie 值贴进去 —— 面板会自动校验、自动为它新建一个通用助手，
确认后写进 `accounts.json`。装好之前请求会返回 `503 no_account`，这是预期行为。

> 首次启动会自动生成一个随机 `api_key` 并写回 config.json —— 网关绝不允许裸奔。
> 它同时是 API 钥匙和面板口令。示例里出现 `testpass` 只是为了说明位置，
> 真部署请换成本机随机串，或只配 `panel_password` 让两者分开。

想用命令行先确认凭据可用（读的是 `accounts.json`，不是 config）：

```bash
python -m polymas2api.cli -c config.json --check
```

调用：

```bash
curl http://127.0.0.1:8787/v1/chat/completions \
  -H "Authorization: Bearer testpass" \
  -H "Content-Type: application/json" \
  -d '{"model":"deepseek-v4.1-flash","messages":[{"role":"user","content":"你好"}]}'
```

---

## 没有账号？从零注册一个

网关本身不含任何账号。先得有一个能登录教学中心的平台账号，**而且它最好不是你自己的正式账号**。

> ### ⚠️ 顺序不能反：先完成机构认证，再抓 `ai-poly`
>
> 账号没有入校（机构认证未通过）时，**平台会把 AI 功能关掉**。这时候抓到的 `ai-poly`
> 在面板里会「看起来正常」——账号加得上、模型列表也列得出来——但**一发消息就失败**。
> 那不是网关坏了，是这个账号还没认证。
>
> 所以要按 **注册 → 认证入校 → 改昵称 → 抓 cookie** 的顺序走，第 5 步务必放在认证之后：
> 认证过程会刷新登录态，认证前抓的那串 token 可能已经作废。

### 为什么建议新注册一个

平台会拿账号资料去跟模型对话：

| 平台行为 | 后果 |
|---|---|
| `getLoginUserInfo` 返回 `userName`，模型开口就叫名字（「XX，来了。」） | 用学校正式账号 = 每句回复都可能带真名 |
| 学校机构下的账号归老师/管理员管，可能被停用，使用记录也在别人那边 | 你的调用不受自己控制 |
| 正式账号带真实学号、专业 | 表单里不想填真的，就没法不填 |

所以路线是：**注册小号 → 进一个免审核的测试机构 → 昵称改成「用户」→ 学号专业填空格**。
这样上游看到的只有「用户」两个字，模型不会叫你的名字，网关的名字清洗功能也就用不上了。

### 步骤

#### 1. 用手机号注册

打开教学中心（`https://hub.polymas.com`；若被跳到别的域名，以实际登录页为准）用手机号注册。注册完平台通常会拿**打码的手机号**
当默认昵称（形如 `198****0665`）—— 第 3 步要把它改掉。

#### 2. 选一个不需要验证的测试型机构

登录后会要求「选择机构 / 学校」。普通学校机构要老师审核或邀请链接才进得去，
**测试型机构直接入校**。列表里找一个名字带「测试」的，例如 **`0904测试机构`**。

> 选错了可以重选；只是选到正式机构可能卡在审核，白等一轮。

**怎么算认证完成**：机构名称出现在你的账号/个人中心里，并且能看到 AI 入口或课程页面。
只有「提交了申请」不算 —— 申请没通过时 AI 依然是关的。

#### 3. 把账号名字改成「用户」

个人资料页里把昵称（接口里的 `userName`）改成 **`用户`**。

这一步最关键：模型的招呼语和自我介绍都从这里取值，它**不是记忆**、也不受客户端控制，
只能在源头改。改成「用户」之后，回复里出现的永远是「用户」。

#### 4. 学号、专业各填一个空格

机构表单要求填学号与专业时，**各填一个空格**即可 —— 表单能通过，上游拿到的也是空白。

#### 5. 抓 `ai-poly` cookie，贴进面板

> **前提**：第 2 步的机构认证已经通过。没通过就先别抓 —— 抓了也用不了。
> 认证后如果 token 变过，重新登录一次再抓，拿到的是当前有效的那串。

1. 浏览器登录教学中心，按 `F12` 打开开发者工具
2. 切到 **Application（应用）→ Cookies → `https://hub.polymas.com`**（没有的话在 `.polymas.com` 那条下找）
3. 找到名为 **`ai-poly`** 的那一条，复制它的 Value（`eyJ...` 开头的一长串）
4. 回到网关面板 →「账号池」→ 右上角 **「＋ 添加账号」** → 粘贴 → 确认

面板会先校验这串 cookie，通过后**自动在这个账号下新建一个「通用助手」**，
把账号和助手一起写进 `accounts.json`。不需要你手动建 agent，也不需要再改配置。

### 验收

添加成功后，面板上这几处应该立刻对得上：

| 位置 | 应该看到 |
|---|---|
| 账号池 | 多出刚加的账号，状态「启用」 |
| 顶部 | `app_code` 有值，助手名「通用助手」 |
| 总览的模型表 | 列出 8~12 个可选模型 |
| 发一句话 | 正常回答，且回复里不出现你的真名 |

**如果发消息失败**（500、身份校验失败、或干脆没反应）：先回网页确认机构认证是否真的通过了，
再重新抓一次 `ai-poly`。账号列表和模型表能显示，只说明 `ai-poly` 本身有效，
**不代表这个账号已经能用 AI**。

> `ai-poly` **等同登录态**：不是密码，但比密码更直接——拿到它的人能以你的身份使用平台。
> 所以它只该待在本机的 `accounts.json`（权限已收紧），不要贴进聊天、issue 或截图。

---

## 控制台面板

```
http://127.0.0.1:8787/panel/
```

口令就是 `api_key` —— 和 workbuddy2api 一样，**一把钥匙同时管 API 和面板**（`panel_password` 只在你想让两者用不同口令时才需要）。当前口令：`testpass`。五个页签：

| 页签 | 内容 |
|---|---|
| **总览** | 请求/成功/失败计数、平均耗时、当前出站指纹、模型目录 |
| **账号池** | 每个账号的调用数、失败数、最近使用、启用/停用 |
| **请求日志** | 最近 200 次调用的元数据（账号/模型/耗时/字节数） |
| **错误汇总** | 按错误消息聚合，看出故障模式 |
| **设置** | 出站画像、节律参数、名字清洗、净化范围、账号池开关；**保存即生效** |

面板带严格 CSP（`default-src 'none'; script-src 'self'`），无内联脚本、无 `innerHTML`，静态资源同源加载。

### 助手行为设置（面板 → 设置 → 助手行为）

这个 agent **认**请求侧的开关，实测有效：

| 设置 | 作用 |
|---|---|
| 启用助手能力 | 关掉会绕过助手的知识库、课程操作与人设，退化成普通模型 |
| 允许联网检索 | 关掉后只查课程知识库 |
| 思考档位 | `none` = 关闭思考通道（不再有 reasoning）；不发送 = 交给平台默认 |

另有「对话模式 / 行为模式 / 身份」（对应上游 `chatMode` / `actMode` / `terminal`），
实测会改变回答风格。

**模型可以切** —— 请求里带 `model` 字段即可（用 modelCode，如 `qwen3.8-max`），
面板「设置 → 模型」也能选。实测确实会换模型：豆包的推理链是**纯中文**，
Qwen / DeepSeek 是英文 —— 这是分类级差异，不是噪声。

> 判据要注意：不能用「模型自述」（会幻觉），也不能用答案长度（课代表的
> system prompt 把自我介绍模板化成固定 76 字符，且平台按用户+agent 存对话
> 历史，每轮 arm 的位置都会影响长度）。可靠的是**推理链的语言**。
> 细节见 `docs/PROTOCOL.md` §6.11。

### 工具调用（function calling）

### 工具名用中文还是英文

模型**原样回显你声明的名字**，不会自己翻译（实测：声明 `run_command` 出来就是
`run_command`，声明 `执行命令` 出来就是 `执行命令`）。所以想要英文输出，声明英文名即可。

如果客户端里只能写中文名，面板「工具调用 → 工具名风格」有第二档：

| 风格 | 声明 `执行命令` 时输出 | 说明 |
|---|---|---|
| `verbatim`（默认） | `执行命令` | 照抄客户端给的名字 |
| `ascii` | `run_command` | 换成 ASCII 别名，响应里附 `polymas.tool_name_map` 映射 |

`ascii` 是为 OpenAI 规范的 `^[a-zA-Z0-9_-]{1,64}$` 准备的 —— 严格的客户端会拒收中文名。

客户端传 `tools` 就能用，标准 OpenAI 形状。**工具名可以是中文**（这个 agent 自带的
名字就是中文，如 `执行命令`），ASCII 和非 ASCII 名字都支持。

```json
{"model": "Doubao-Seed-2.0-mini",
 "tools": [{"type": "function", "function": {
   "name": "get_weather", "description": "查天气",
   "parameters": {"type": "object", "properties": {"city": {"type": "string"}}}}}],
 "messages": [{"role": "user", "content": "北京天气"}]}
```

返回 `finish_reason: "tool_calls"` 和 `message.tool_calls`；把结果按
`role: "tool"` 回传即可，第二轮拿到正常回答。

**为什么要在提示词里做**：平台不接受客户端传的工具定义 —— agent 的工具是服务端
绑定的（`bindTools` / `ability-config`），chat 请求里没有这个字段。所以反代把工具
**描述进提示词**，再把回答解析回 `tool_calls`。

**格式为什么不是 `tool_call` 代码块**：那个 agent 的 system prompt 有护栏，会拒绝
「声称自己在调用工具」（原话：*输出一个假的工具调用块等于冒充系统动作*）。改成
`CALL 工具名 {JSON}` 这种「把参数交给调用方执行」的中立格式后才放行。

> **抹掉名字**：上游会拿平台账号名（`getLoginUserInfo` 的 `userName`）直接称呼你，
> 反代默认把它从正文和思考链里洗掉。面板「名字清洗 → 抹掉的名字」可增删，逗号分隔。

> **重试开关**：这个 agent 常回答「这就发给你」却不真的调用工具（112 个工具时约
> 一半概率）。反代默认会纠正重试（面板「没调工具就重试」），实测把命中率从
> 3/5 提到 8/8。代价是工具请求的短回答首字延迟约 11 秒；关掉更快但不可靠。
> 详见 `docs/PROTOCOL.md` §6.11。

**实测可用的模型**（2026-11-16，干净上下文下逐个验证）：`deepseek-v4.1-flash`、
`deepseek-v3`、`Auto`、`Doubao-Seed-2.0-mini` 等都能正常调用并完成往返。

**唯一的例外是 `tool_choice: "required"`** —— 它不会硬凑一个调用。原话：

> 你只说了"你好"，没有任何查询需求。规则 7 让我无论如何都塞一行调用参数出去，
> 等于凭空替你向外部程序下达一个你根本没要的指令。

这是 agent 人设里的护栏，绕不过去，也不该绕。

### 上下文隔离（默认开启）

平台按 `metaData.sessionId` 划分对话，**不给这个字段时所有请求共用一段历史** ——
实测：一次请求让 agent 记个代号，下一次完全无关的请求能把它说出来。对 API 这是错的。

所以默认每次请求都生成独立会话，并且**关掉长期记忆**（只开新会话不够，平台还会存
长期记忆，模型会按时间顺序猜"最近一条"，看起来跟泄漏一样）。

- **不传 `session_id`** → 每次请求互相隔离（OpenAI 语义：客户端自带历史）
- **传 `session_id`** → 同一 id 共享上下文，多轮正常

面板「工具调用 → 请求间隔离上下文」可以关掉。

> ### ⚠️ 安全提示：这个反代能驱动上游执行命令
>
> agent 自带 `执行命令` 工具，API 侧一句话就能让它以 **root** 在平台容器里跑
> shell，而且容器**能出网**。实测（无害探测）：`whoami` 返回 `root`，
> `curl https://api.github.com` 返回 `200`。
>
> 能挡住的只有 agent 自己的提示词约束（它拒绝 `/tmp`、拒绝 `ls /`），**不是内核
> 隔离**。所以：**别把 8787 暴露在公网**，口令别用 8 位短串，并且盯住请求日志的
> `tools` 字段。细节见 `docs/SECURITY.md` §0.2。

### 上游自己的工具调用会可见

agent 用它的工具时，上游会发 `TOOL_REQUEST` / `TOOL_RESPONSE`，反代原来**直接丢掉**，
所以你只看到对话消息。现在它们出现在响应的 `polymas.upstream_tools` 里
（流式下每帧一个 `polymas.upstream_tool`），可以看到 agent 调了哪个工具、参数和结果。

这些**不会**变成 `tool_calls` —— 它们已经在服务端执行完了，让客户端去执行一个它
从没听说过的工具是错的。

### 名字清洗

上游 agent 的 system prompt **锁在服务端**，请求侧改不掉（实测过换字段、注入角色
扮演、翻转 chatMode/actMode/terminal，全部无效）。

**人格净化已移除（2026-11-17）。** 它把「我是你的电子工艺实习小管家」改写成中性
自称，但换账号之后一条规则都匹配不上（新 agent 说「通用助手」），却会误伤——
角色规则允许「小管家」前面有 14 个任意字符，于是「你可以问问班级小管家。」被整句
替换成「AI 助手。」。收益为零、风险为负，整块删掉了。

留下的是**名字清洗**：平台会把账号名（`getLoginUserInfo` 的 `userName`）下发给
agent，它会直接喊出来。在「设置 → 名字清洗 → 抹掉的名字」里填上要抹掉的称呼即可
（逗号分隔），正文与思考链都会处理。当前账号的 `userName` 是「用户」，所以留空、
模块处于休眠状态。

细节见 `docs/SECURITY.md` §0.1 与 `docs/PROTOCOL.md` §6.9、§6.11。

---

## 配置

**`config.json` 只放设置，不放任何凭据** —— 账号在 `accounts.json` 里，由面板管理。
（从旧版本升级的配置里可能还留着 `ai_poly` / `user_nid` / `friend_nid`，启动时会
一次性迁移进 `accounts.json`，之后就不再读它们了。）

```json
{
  "model": "deepseek-v4.1-flash",
  "host": "127.0.0.1",
  "port": 8787,
  "api_key": "testpass",
  "panel_password": "testpass",
  "fingerprint": "edge-157",
  "send_origin": true,
  "min_interval_ms": 800,
  "jitter_ms": 700,
  "think_time_per_char_ms": 4.0,
  "sanitize_scope": "off",
  "retain_long_term_memory": false,
  "pool_enabled": true,
  "accounts_file": "accounts.json"
}
```

### 凭据

只有一个：**`ai-poly`**（HS256 JWT，教学中心签发）。

- **怎么加**：面板 → 账号池 → 「＋ 添加账号」→ 贴进去。面板会先校验，通过后
  自动为它新建一个通用助手，两个 nid 一起写进 `accounts.json`。
- **从哪来**：浏览器 F12 → Application → Cookies → `.polymas.com` → `ai-poly`。
- 请求侧同时接受 `Cookie:` 头与裸 `Authorization:` 头（**无 `Bearer` 前缀**）。

> 早期版本还支持过 `jt-cas`（ES256 CAS cookie）。已移除：平台只用 `ai-poly`
> 就能续期，`jt-cas` 反而带来 24 小时过期的额外负担。

### 凭据会过期

`ai-poly` **不带 `exp` 声明**，过期时间由平台判定 —— 只有被拒绝时才知道。

- 每个账号**独立续期**：走 `GET /oauth/token/getToken?userNid=<该账号的 nid>`，
  只有新 token 通过校验才会换上，失败时保留旧 token 且只影响该账号。
- 默认每 **360 分钟**续一次（`renew_interval_minutes`），面板「凭据与续期」区
  显示每个账号的上次续期时间与续期次数，也可以点「立即续期」。
- 被平台拒绝时网关返回 `身份校验失败，请刷新页面后重试`，该账号进入隔离，
  面板状态变红，日志里打 WARNING。

### 反侦测参数

| 键 | 默认 | 作用 |
|---|---|---|
| `fingerprint` | `edge-157` | 出站客户端画像：`edge-157` / `chrome-131` / `chrome-131-noorigin` |
| `send_origin` | `true` | 是否发 `Origin: https://hub.polymas.com` |
| `min_interval_ms` | `800` | 两次上游调用的最小间隔 |
| `jitter_ms` | `700` | 每次调用叠加的随机延时 |
| `think_time_per_char_ms` | `4.0` | 按输入长度模拟人工输入耗时 |
| `sanitize_scope` | `off` | 请求体净化范围（`off` / `identity` / `all`） |

> **关于 `sanitize_scope`**：workbuddy2api 靠这套规则躲开上游的逐字内容审核。polymas **实测不做这种拦截**（见 PROTOCOL.md §6），所以默认关闭。打开也无害，但没有收益。

### 多账号

**从面板加，不要手改文件。** 每点一次「＋ 添加账号」就多一个账号，贴进去的那串
`ai-poly` 决定它是哪个用户，网关会顺手给它新建一个通用助手。

面板上每个账号可以：

| 操作 | 说明 |
|---|---|
| **权重** | 权重越大被抽中的概率越高（平滑加权轮转，不是随机）；旁边显示实际占比 |
| **备注** | 自由填写的名字，如「主号 / 小号」，纯显示用 |
| **停用 / 启用** | 临时摘掉一个账号，不用删 |
| **删除** | 连同它的凭据一起删；删空也允许，删空后请求返回 `503 no_account` |
| **立即续期** | 只续这一个账号，失败不影响其他账号 |

失败的账号会被隔离一段时间，避免一个失效 cookie 拖垮整个网关；所有账号都失效时
网关仍会拿一个去重试，而不是直接拒绝请求。

`accounts.json` 是这个网关**唯一**的凭据来源，权限已收紧（Windows 上用 icacls
限制为「当前用户 + SYSTEM + Administrators」）。备份它，就等于备份了凭据。

---

## 接口

| 路径 | 说明 |
|---|---|
| `GET /health` | 存活检查（不鉴权） |
| `GET /v1/models` | 该 agent 的模型目录 |
| `POST /v1/chat/completions` | 对话（`stream: true` 走 SSE） |
| `POST /v1/session/new` | 清空小真的记忆窗口 |
| `GET /panel/` | 控制台面板 |
| `GET /panel/api/*` | 面板数据接口 |

流式 chunk：

```json
{"id":"chatcmpl-polymas","object":"chat.completion.chunk","created":1794488023,
 "model":"deepseek-v4.1-flash",
 "choices":[{"index":0,"delta":{"content":"你好"},"finish_reason":null}]}
```

思考过程走 `delta.reasoning_content`（与 DeepSeek 约定一致）。

---

## 已知限制

- **`usage` 恒为 0** —— 平台不返回 token 用量。
- **`id` 恒为 `chatcmpl-polymas`** —— 平台不返回请求 id。
- **模型切换会被服务端忽略** —— 该 agent 在 DB 里锁死为 `deepseek-v4.1-flash`；强行传别的 `modelCode` 平台会照办，但人格与护栏会变（实测 `qwen3.8-max` 会漏出英文推理链）。
- **助手有长期记忆** —— 同一 `user_nid` + `friend_nid` 共享上下文，`/v1/session/new` 可清空。
- **凭据过期** —— cookie 失效后上游返回「身份校验失败」，需重新导出。面板的「错误汇总」页会显示这类错误。

---

## 协议还原

见 [docs/PROTOCOL.md](docs/PROTOCOL.md)：端点、请求体、认证、SSE 事件格式、TLS/ALPN 实测结论，每条结论都附可复现证据。

---

## 开发

```bash
python -m pytest tests -q                              # 37 个单元测试，不需要网络
node --check polymas2api/web/app.js                    # 前端语法
python -m polymas2api.cli -c config.json --check       # 联调校验
```

MIT License.

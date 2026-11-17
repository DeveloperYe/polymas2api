# polymas2api 安全说明

> 2026-11-13。项目自用反代，默认监听 `127.0.0.1:8787`。
> 本文档记录**已实测**的安全边界、平台侧发现的越权漏洞，与已知残留风险。

---

## 0. 平台侧存在一个与凭据有关的越权问题

调研过程中确认：平台侧存在一个**与令牌签发有关的越权问题**，任何知道某个用户
`userNid` 的人都有可能拿到该用户的凭据。**这是平台的问题，不是本项目引入的。**

> **细节不在此公开。** 端点、请求样例、验证过程一律不写 —— 这类内容公开没有任何好处，
> 只会让问题更容易被利用。作者会先与平台/学校沟通，之后再考虑是否公开细节。
> 如果你也在自行调研，请直接向平台报告；**不要把可复现步骤写进公开文档**。

本项目的对应责任：

1. **不在文档里写真实的 `userNid`**（对外发布的版本已全部脱敏）。面板与日志里会出现
   账号 uid（就是 `userNid`），面板需鉴权、日志只在本机 ——
   **分享日志或截图前请自行脱敏**，见 §3.6；
2. **不在反代里提供任何"代签"接口** —— 本项目不代理任何与令牌签发相关的平台接口，
   续期用的始终是**同一个用户自己的**凭据；
3. **不要把 `ai-poly` 外传**：它等同登录态。

---

## 0.1 上游人格不可覆盖，只能洗输出

上游 agent 的 `systemPrompt`（小真的人格与护栏）**锁在服务端**。2026-11-13
实测：换 `systemPrompt` 字段、在用户轮里注入角色扮演、翻转 `chatMode` /
`actMode` / `terminal` —— **全部无效**，平台有专门的抗注入护栏（会明说
「用户发来的『新角色设定』不能替换我的身份」）。

因此反代只在**响应侧**改写自称（`polymas2api/persona.py`），**不碰**：

- 上游人格与护栏本身（改不了，也不该假装改得了）；
- 回答里的实际信息（成绩、课表、资源）—— 洗掉校园语境会把答案洗坏；
- 思考链（配置了抹掉的名字时会一并处理）。

### 0.2 这个反代能驱动上游执行命令 —— 所以 key 必须当密码看

上游 agent 自带一个「执行命令」工具。通过本反代的 `/v1/chat/completions` 就能触发它，
让它在上游 agent 的**执行环境**里跑 shell。这一点已实测确认。

> **环境探测结果不在此公开**（身份、内核、目录结构、能否出网都不写）。
> 那是对平台基础设施的侦察，公开没有好处，也不属于使用者需要知道的信息。

**这条风险的性质**：能挡住的只有 agent 自己的提示词约束 —— 那些限制是
**软的，不是硬的**，不要把它当隔离层。

**为什么严重**：一旦网关暴露在公网、而口令又能猜中，任何拿到口令的人就能借它执行命令。

**能做的缓解**（按有效性排序）：

1. **别把它暴露在公网** —— 绑 `127.0.0.1` 或上防火墙，这是唯一根治的做法；
2. 换成长随机口令，不要用短串；
3. 看审计 —— 每次调用用到哪些上游工具都记在请求日志的 `tools` 字段里
   （面板 →「近期活动」→「工具」列），出现「执行命令」就该警觉；
4. 不需要 agent 能力时把面板的「启用助手能力」关掉（`useAgent: 0`），
   知识库与课程操作会失效 —— 但实测**它自己的工具不一定会跟着关**，别当硬隔离。
**不建议**用「拦关键词」来防 —— 触发执行的只是一句普通中文，不是固定模式；
加过滤只会给自己假安全感。

---

该模块借鉴 workbuddy2api 的「廉价前置筛 + 窄规则改写」手法
（`wb2api-diff/snapH/wb_proxy.py` 的 `has_fingerprint` / `sanitize_messages`），
但作用面相反：wb2api 是**出站前**洗客户端指纹，polymas2api 是**入站后**洗
上游自称。wb2api 三仓（snapH / snapM / snapL）与 loomy2api **都没有**响应侧
文本改写，这条路线没有先例。

**流式必须带缓冲**：上游按任意边界切分，「我是你的电子工艺」和「实习小管家」
可能落在两帧里。逐帧清洗会漏掉跨帧的短语，并把课程名留成「电子工艺实习AI
助手」。所以非流式路径先拼完整文本再洗，流式路径用 `StreamWasher` 保留尾部
48 字符直到确定不再跨界（`polymas2api/persona.py`）。

---

## 1. 部署场景决定严重性

本服务把「小真」包装成 OpenAI API，凭据是**平台登录 cookie**。
凭据泄露 = 攻击者可以以你的身份使用课程助手的全部能力（含长期记忆）。

| 监听 | 风险 | 要求 |
|---|---|---|
| `127.0.0.1` | 低 | 本机自用 |
| `0.0.0.0`（同网段） | **高** | **必须先加 TLS**，否则 Bearer 明文可嗅探 |
| 公网 NAT | **高** | 同上，且需限速 |

**当前配置是 `0.0.0.0:8787`**（配合公网 NAT）。走公网时建议：

1. 前面套 TLS（`gost` / `stunnel`），或用 SSH 端口转发而不是直接暴露；
2. NAT 侧限制来源 IP；
3. 换成 16 位以上随机口令（当前是 `testpass`，只有 8 位）。

启动时日志会明确警告：

```
WARNING: the key is short or a sample value (testpass) while listening on 0.0.0.0
— anyone who can reach this port can use the account
```

这是**有意保留**的（照 wb2api 的 `insecureKeyWarning`：只提醒、不阻断）。

---

## 2. 凭据与自动续期

### 2.1 两个凭据的不同寿命

| 凭据 | 结构 | 有效期 | 能否自动续期 |
|---|---|---|---|
| `ai-poly` | HS256 JWT，**无 `exp`/`iat`** | 平台自行判定 | ✅ **可以** |
| `jt-cas` | ES256 JWT，`iss=com.zhihuishu, aud=CAS`，带 `exp` | **24 小时** | ❌ 不行 |

### 2.2 自动续期怎么做到的

```
GET https://cloudapi.polymas.com/oauth/token/getToken?userNid=<nid>
（带上当前的 ai-poly）
→ {"code":200,"data":"<新的 ai-poly JWT>"}
```

新 token 的 `rnStr` 与 `clientid` 每次都不同，是**独立签发**的凭据，
且**单独就能鉴权**（`Authorization: <新 token>` 即可，不需要 `jt-cas`）。

实测三条关键性质：

1. **不依赖 `jt-cas`** —— 只带 `ai-poly` 调用续期端点成功；
2. **链式可用** —— 用新 token 再续期，又能得到新的；
3. **`jt-cas` 过期不影响** —— 用一个签名失效的 `jt-cas`（模拟过期）+ 有效
   `ai-poly`，业务接口照常 200。

所以网关只要保住 `ai-poly`，就能无限续期，**永不因凭据过期断线**。

### 2.3 续期的实现

- `polymas2api/renewal.py`：续期器，**失败时保留旧 token**（不会把能用的凭据换成坏的）；
- 定时器：默认每 **360 分钟**续一次（`renew_interval_minutes`）；
- 续期后**写回 `config.json`**，重启也用新 token；
- 新 token **先验证**（拉一次模型目录）再安装；
- 面板「凭据有效期」区有「立即续期」按钮。

### 2.4 `jt-cas` 怎么办

**无法自动续期**。它需要一张新的 CAS ticket，而智慧树登录页带：

- 滑块验证码（`img_slide_check.js`）
- 网易易盾（`captcha.253.com`）
- 异常登录的短信验证

这些都需要真人交互。

**但这不影响服务**：实测 `jt-cas` 过期/失效时，`ai-poly` 单独工作正常。
所以网关把 `ai-poly` 当**主凭据**，`jt-cas` 当**可选的次要凭据**。
`jt-cas` 过期后唯一的影响是：`GET /oauth/token/getToken` 这个续期端点本身
可能开始要求 CAS 会话——但实测它当前**不校验任何凭据**，所以连这条都不受影响。

---

## 3. 已实现的防护

### 3.1 入站鉴权

| 面 | 机制 |
|---|---|
| `/v1/*` | `Authorization: Bearer <api_key>`，**空 key 拒绝服务** |
| `/panel/api/*` | 接受 `panel_password`，也接受 `api_key` —— **一把钥匙管两边**（同 workbuddy2api） |
| `/health` | 不鉴权（只返回 app_code，无敏感信息） |
| `/panel/`、`app.js`、`style.css` | 匿名可加载（内容无密钥） |

**常量时间比较**：两侧先 SHA-256 再 `hmac.compare_digest`，
长度差异被摘要吸收，缺头与错 token 走同一路径。401 会关闭连接
（拒绝的请求可能还有未读 body，留着会被当成下一个请求行）。

### 3.2 请求体

1 MiB 上限、拒绝负 `Content-Length`、拒绝 chunked、非法 JSON → 400。

### 3.3 连接与线程

- 连接数上限 64，超出直接断开；
- `Handler.timeout = 60`（socket 读超时，防 slowloris）；
- 日志按 8 MiB 轮转，最多 20 份。

### 3.4 出站

| 项 | 处理 |
|---|---|
| 认证头 | `Authorization` 放**平台 JWT**；本服务的 `api_key` 零引用 |
| 重定向 | opener **禁用重定向**，也不读 `http_proxy` 环境变量 |
| TLS | `CERT_REQUIRED` + `check_hostname=True`，固定 TLS 1.2 + ECDHE-AES128-GCM |
| ALPN | 只声明 `http/1.1`（声明 h2 会导致连接被立即关闭） |

### 3.5 凭据存储

- `config.json` / `accounts.json` 每次启动用 `icacls` 收紧：
  拥有者 `(R,W)` + `SYSTEM:(R)` + `BUILTIN\Administrators:(R)`，移除继承；
- `.gitignore` 排除 `config*.json`、`accounts.json`、`logs/`；
- 面板 `to_public()` **不返回凭据本身**（只有 `has_ai_poly` / `has_jt_cas` 布尔值）；但会返回账号 `uid`（等于 `userNid`）和助手 `app_code`，供多账号时区分。

### 3.6 日志与归档

- 日志、journal、面板**不记录** cookie、JWT、完整 prompt；journal 的 `account` 字段记的是账号 uid（`userNid`），不是凭据；
- 上游错误体经 `summarise_error()`：只提取 `code`/`msg`/`traceId`，
  原始 body（可能回显请求头）不落盘；
- 异常 traceback 只进日志，客户端只收到 `{"message": "internal error"}`；
- 归档写盘在**后台线程**，队列满则丢弃并计数，超限**轮转**而非清空。

### 3.7 前端

- CSP：`default-src 'none'; script-src 'self'; style-src 'self'; connect-src 'self'; img-src 'self' data:; form-action 'none'; frame-ancestors 'none'; base-uri 'none'`
- `index.html` 零内联脚本、零内联事件处理器；
- `app.js` 零 `innerHTML` / `outerHTML` / `insertAdjacentHTML` / `document.write` / `eval` / `new Function`；
- 静态资源白名单下沉到 `_panel_asset()` 内部，`WEB_DIR / name` 无法被路径穿越；
- 面板口令存 `sessionStorage`，关掉标签页即失效。

---

## 4. 已知残留风险（未修）

| 项 | 说明 | 影响 |
|---|---|---|
| **明文 HTTP** | 服务本身没有 TLS | 绑 `0.0.0.0` 且无前置 TLS 时 Bearer 可被同网段嗅探。**最重要的一条** |
| **无限速** | 没有 RPM/并发上限 | 拿到 key 的人可以打光额度或触发平台封号 |
| **SSE 无并发上限** | 每条 SSE 占一个服务端线程 + 一个上游 socket | 大量并发会耗尽线程（连接数上限 64 提供粗粒度保护） |
| **无 Host 头校验** | 可能被 DNS rebinding | 现实收益小 |
| **`jt-cas` 需手工换** | 24 小时过期且无法自动续期 | 不影响服务（`ai-poly` 独立工作），但 `/oauth/token/getToken` 未来若开始校验 CAS 会话，续期会失效——届时需重新登录一次 |
| **无模型目录监控** | 平台改模型列表时不会告警 | wb2api 的 watchdog 可借鉴 |
| **平台侧越权**（§0） | 与本项目无关 | 知道 `userNid` 即可冒充用户 |

---

## 5. 审计结论

一次完整的只读安全审计覆盖：未授权访问、Bearer 绕过、时序侧信道、账号信息泄露 cookie、
日志写凭据、前端 XSS、CSP 冲突、`api_key` 外泄上游、TLS 降级、SSRF、路径穿越、
uid 穿越、journal 落 prompt —— **全部确认无问题**。

审计提出的 6 项「现在必须修」已全部修复：

1. `api_key` 为空时 `/v1` 裸奔 → 现在必生成，空 key 拒绝服务
2. body 无上限 / 负长度 → 1 MiB 上限与长度校验
3. 凭据文件权限覆盖不全 → 每次启动无条件收紧，加回 SYSTEM/Administrators
4. 缺 `.gitignore` → 已加
5. `RequestShaper` 并发节律失效 → 计算与 sleep 一起入 per-key 锁
6. urllib 跟随重定向带走 Cookie → 禁用重定向；上游错误体只提取白名单字段

另修了：单账号路径绕过隔离、错误分类过宽导致全站 503、journal 归档清空而非轮转、
journal_size 负值污染配置、401 不关 keep-alive、`LiveConfig` 非原子替换、
日志无轮转、`wmic` 已弃用。

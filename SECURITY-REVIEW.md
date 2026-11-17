# polymas2api 安全与隐私审查报告

> 审查日期：2026-11-18
> 审查对象：本目录（`polymas2api-clean`，干净版，33 个文件）
> 对照对象：实际在跑的工作副本 `../polymas2api`（含凭据与运行日志）
> 判断口径：**已验证** = 本轮实测过；**推测** = 由代码推断、未实测。

---

## 0. 结论

**可以对外发布。** 没有发现可被远程利用的漏洞：三个入口全部鉴权、路径穿越打不通、
无动态执行、出站 TLS 校验证书。**本次审查中发现并已修掉 8 个隐私/配置/一致性问题**，
其中三个是发布阻断级：真实姓名写进了源码与文档（约 80 处）、照抄示例配置会导致面板被
自己锁死、README 教的还是改造前的「把 cookie 填进 config.json」。剩下 7 条是"能更好但
没有被攻破"的改进项，以及 4 条架构性风险（明文 HTTP 等），都列在下面。

---

## 1. 本次已修复

| 严重度 | 位置 | 问题 | 证据 |
|---|---|---|---|
| **高** | `polymas2api/persona.py:5,26,45,48,66,71,82,155,186,190,192,227,237,238`；`polymas2api/config.py:113`；`docs/PROTOCOL.md:1744,1746,1754,1762,1763,1765,1769`；`tests/test_gateway.py`（约 50 处） | **真实姓名「<真实姓名>」被写进源码注释、文档与测试数据**，约 80 处。对外发布等于公开真实姓名。 | 全量扫描命中 4 个文件；已替换为占位名「林晓东」，替换后再扫 0 残留，235 个测试仍全过 |
| **中** | `polymas2api/config.py:366`（原 `write_example`），新增处理在 `config.py:270-275` | 示例配置用 `<>` 写说明，但加载时被当成**真实值**：`api_key` 实际变成 `<optional; clients must send it when set>`，照抄示例的人会发现面板怎么都进不去。 | 实测 `load_config(config.example.json)` → `api_key` 长度 47 且含 `optional`；修复后同一文件得到随机 `sk-…`（35 位）并被回写 |
| **低** | `polymas2api/server.py:1486-1492` | `Server` 响应头暴露解释器版本，便于攻击者挑对应 CVE。 | 修复前实测 `Server: BaseHTTP/0.6 Python/3.14.9`；修复后 `Server: polymas2api` |
| **低** | `docs/SECURITY.md:37,244,248` | 文档声称「面板只显示 `has_ai_poly` 布尔值」「日志不记录 userNid」，与实现不符（面板账号表与 journal 都显示 uid = userNid）。安全文档说错比没有更危险。 | 已按实际行为改写三处 |
| **高** | `README.md:5,23-31,196-267`；`polymas2api/cli.py:58-62` | **README 与 CLI 都还停在改造前**：快速开始教人「填入两个 cookie 值和 id」，而示例配置已无这些字段；凭据章节整段在讲已移除的 `jt-cas`（24 小时过期那套）；多账号说手改 `accounts.json`；`--check` 只读 config 旧字段，正确安装下**永远报「config is incomplete」**。 | 照做的人第一步就卡住；`--check` 给出错误结论 | 已重写 README 五段（快速开始 / 配置 / 凭据 / 凭据过期 / 多账号），`--check` 改为读账号池；实测 `--check` 输出 `credential check passed` 并列出 8 个模型 |
| **中** | `polymas2api/config.py`（`retain_long_term_memory: bool = True`） | **长期记忆默认开着**：新装用户不配置的话，每一句对话正文都会进入平台侧长期记忆，且换 `sessionId` 也能被下一会话召回。对"别人拿去用"的产品，这属于替用户默认选择了隐私代价。 | 默认即发生，用户不会察觉 | 实测 dataclass 默认 `True`、线上 config 为 `False`；已改为默认 `False`（opt-in），面板开关说明同步 |
| **低** | `polymas2api/config.py:218`（`Config.missing()`） | 改造后已成死代码——只检查旧的 config 凭据字段，唯一引用是 `cli.py` 里的一句注释。留着会让后来者以为凭据还在 config 里。 | 无直接风险，误导维护者 | 全库 grep 无调用方；已删除 |

> 另外，工作副本的文档里还散落着真实标识（`<agent-nid>`、`<agent-nid>`、`<userNid>`、
> `<旧口令>`、真实 IP）。已把 `docs/*.md` 与 `README.md` 一并换成占位值，
> `config.json` / `accounts.json` 未动（那是线上真实凭据）。

---

## 2. 未修复的发现

### 2.1 隐私相关

| 严重度 | 位置 | 问题 | 可利用性 | 证据 |
|---|---|---|---|---|
| **中** | `polymas2api/journal.py:67`（`"account": self.account`）；`server.py:292,1207,1386` 的日志行 | journal 归档与运行日志里记录的 `account` **就是 `userNid`**。`SECURITY.md §0` 记载：知道 `userNid` 即可在平台侧换出有效 token，所以它是敏感性标识。 | 需要本机文件读取权限；但**分享日志、贴截图、提交 bug 时就会带出去** | 实测 `logs/requests.jsonl` 命中 12 处、`logs/gateway.log` 命中 8 处 |
| **低-中** | `polymas2api/attachments.py:59`、`:164` | 图片附件落在 `%TEMP%\polymas2api-attachments`，`resolve()` 后**路径含 Windows 用户名**；而该路径被原样写进 prompt 发给平台（`本地路径：C:\Users\<用户名>\AppData\...`）。 | 平台侧与 agent 的工具侧都能看到本机用户名 | 实测 `attachment_dir()` = `C:\Users\<用户名>\AppData\Local\Temp\polymas2api-attachments`，`describe_image()` 输出含该绝对路径。**已决定保留**（2026-11-18）：路径必须是真实绝对路径，模型侧工具才能打开文件；平台本来就知道用户身份，此项只是多一个本机用户名。需要时可用环境变量 `POLYMAS_ATTACHMENT_DIR` 指到不含用户名的目录。 |
| **低** | `polymas2api/attachments.py:36,74-84` | 图片默认保留 24 小时，但清扫只在**写入新图时**触发（每进程最多每小时一次）。闲置实例上的图会一直留着。 | 本机其他进程 / 备份 / 云同步可读取 | 代码：`_sweep_occasionally()` 仅在 `store_bytes()` 里调用 |
| **低** | `polymas2api/journal.py:63-84` | 归档记录 `trace_id`（平台侧追踪 id），可与平台侧会话关联。 | 仅本机文件 | `to_dict()` 字段表 |

### 2.2 接口面

| 严重度 | 位置 | 问题 | 可利用性 | 证据 |
|---|---|---|---|---|
| **低** | `polymas2api/server.py:1605-1608` | `/health` **无需鉴权**，且返回助手 `app_code`。 | 只能拿到 agent nid，无法冒充用户（冒充需要 `userNid`）；可用于确认"这里是 polymas2api" | 干净版空池实测 `{"status":"ok","app_code":""}`；配好账号后即返回真实 agent nid |
| **低** | `polymas2api/server.py:1615-1623` | `/panel/` 的 HTML/JS/CSS **无需鉴权**即可下载。 | 仅静态前端，无数据；面板 API 仍要钥匙 | 干净版无 key 实测 200、10943 字节 |
| **中** | `polymas2api/server.py`（全文件） | **没有任何速率限制或失败锁定**。认证本身是纯哈希比较，但可以无限次尝试。配合 8 位短钥匙（见 §4）风险显著放大。 | 拿到端口即可暴力猜测 | 全库 grep `rate/limit/throttle/lockout` 无相关实现 |
| **低** | `polymas2api/upstream.py:233,244` | 同一凭据**发两次**：`Authorization: <JWT>` 与 `Cookie: ai-poly=<JWT>`。 | 不扩大泄露面（同一主机、同一 TLS 连接），但抓包/中间件日志里多一处明文 | 代码两行 |

---

## 3. 已验证安全（逐项实测）

| 检查项 | 结果 | 证据 |
|---|---|---|
| 三面鉴权 | `/v1/models`、`/v1/chat/completions`、`/panel/api/*` 无 key 全部 **401** | 在干净版实例（8792）实测 |
| 错误钥匙 / 空 Bearer | 全部 **401** | `Bearer wrong`、`Bearer `（空）均 401 |
| 空钥匙语义 | 未配置钥匙时**从不放行**（不会变成"开放"） | `server.py:1936-1949`；单测 `test_verify_bearer_never_authorises_without_a_configured_key` |
| 恒定时间比较 | SHA-256 摘要 + `hmac.compare_digest`，长度不参与计时 | `server.py:1944-1949` |
| 路径穿越 | `/panel/../config.json`、`..%2f`、`%2e%2e/`、多层 `../`、`/panel/config.json` 五种变体全部 **404** | 静态资源走白名单 `PANEL_ASSETS`（`server.py:1687`），文件名不匹配直接 404 |
| CSP 与响应头 | `default-src 'none'; script-src 'self'; style-src 'self'; connect-src 'self'; img-src 'self' data:; form-action 'none'; frame-ancestors 'none'; base-uri 'none'` + `nosniff` + `X-Frame-Options: DENY` + `no-referrer` | `curl -D -` 实测响应头 |
| 无跨域放行 | 完全没有 `Access-Control-Allow-Origin`，浏览器默认阻止跨源读 | 全库无 CORS 头 |
| 出站 TLS | `ssl.create_default_context()`（**默认校验证书**）+ TLS 1.2 锁定 + `server_hostname=host`（SNI + 主机名校验） | `fingerprint.py:238-257`、`upstream.py:264` |
| 动态执行 | 无 `eval` / `exec` / `pickle` / `yaml.load` / `__import__` | 全库 grep |
| 子进程 | 只有一处 `subprocess.run`，参数为固定 `icacls` 调用，无 shell | `config.py:351-357` |
| 凭据文件权限 | `accounts.json`、`config.json` 的 ACL 已收紧：`Administrators:(R)`、`SYSTEM:(R)`、当前用户 `(R,W)` | `icacls` 实测输出 |
| 设置写入白名单 | 面板只能改 25 个白名单键，**改不了 `host`/`port`/`api_key`/`panel_password`**；数值有上下界 | `server.py:1389-1420` |
| 请求体防护 | 有大小上限、拒绝负数 `Content-Length`、拒绝 chunked、非 JSON 回 400 | `server.py:1563-1596`；实测非 JSON → 400 |
| 错误不外泄 | 未捕获异常只回 `{"message":"internal error"}`，traceback 只进日志 | `server.py:1680` |
| 无 SSRF | `image_url` 的 `http(s)` 链接**不会被网关抓取**，只把 URL 原样写进提示 | `attachments.py:214-220`；代码路径确认无出站请求 |
| 归档不含正文 | journal 只存字符数/字节数/耗时/账号/模型，无 prompt、无回答 | `journal.py:63-84` 字段表；实测日志无连续汉字 |
| 出站无本机身份 | 请求头是通用 Chrome-on-Windows 指纹，不含主机名、用户名、内网 IP | `fingerprint.py:43-52` |
| 配置占位符 | `<>` 形式的说明文字不再被当成真实值 | `config.py:270-275`；实测见 §1 |
| 干净版无数据残留 | 无 `accounts.json`、`config.json`、`logs/`、`*.pid`、`.bak`、截图 | 目录清单 |

---

## 4. 架构性风险（不是缺陷，是部署前提）

| 风险 | 说明 | 缓解 |
|---|---|---|
| **明文 HTTP** | 网关只监听 HTTP，钥匙与凭据在网络上明文传输。`SECURITY.md §4` 已述。 | 只在回环使用，或前置带 TLS 的反向代理 |
| **平台侧越权** | 知道任意用户 `userNid` 即可在该平台换出有效 token，**与本项目无关**。 | 不公开 `userNid`；建议向学校/平台报告（`SECURITY.md §0`） |
| **一把钥匙管两边** | `api_key` 同时是 API 钥匙与面板口令，泄露即两者皆失。 | 配 `panel_password` 分开两条口令 |
| **短口令** | 8 位口令在无速率限制的情况下可暴力猜测。启动时只有一条 WARNING，不阻断。 | 用 16 位以上随机串（`--print-example` 生成的随机钥匙默认 35 位） |

---

## 5. 干净版包含 / 不包含

**包含**：`polymas2api/` 源码（20 个文件）、`tests/`（3 个文件，235 个用例）、
`docs/PROTOCOL.md`、`docs/SECURITY.md`、`README.md`、`pyproject.toml`、
`gateway.ps1`、`start.cmd`、`stop.cmd`、`.gitignore`、`config.example.json`。

**不包含**（已核实不在目录里）：

| 排除项 | 原因 |
|---|---|
| `accounts.json`、`config.json`、`config.json.bak-*` | 含凭据 |
| `logs/`（`requests.jsonl`、`gateway.log`） | 含账号 uid、平台 trace_id |
| `tools/`（探测脚本 + 9 张面板截图） | 脚本含硬编码 nid；**截图含真实公网 IP、账号 uid、agent nid** |
| `docs/*.png`、`tools/shots/*.png` | 同上 |
| `__pycache__`、`.pytest_cache`、`*.pid` | 运行时产物 |

脱敏替换表（真实值 → 占位值）：`userNid` → `AbCdEfGhIj`；两代 agent nid →
`AgEnTnId01`；`api_key`/`panel_password` → `testpass`；公网 IP → `gateway.example.com`；
内网 IP → `192.168.1.10`；主机别名 → 去掉；本机路径 → `C:\polymas2api`；
真实姓名 → `林晓东`；学校机构名 → `<学校机构>`。

---

## 6. 验证方式

在干净版目录内执行：

```
python -m pytest tests -q                    # 235 passed
python -m polymas2api.cli -c config.example.json --port 8791
```

实测结果：235 个测试全过；用自带的无凭据示例配置可以全新启动，`/health` 正常、
账号池为空、请求返回 `503 no_account`（符合"新装即空"的设计）。

隐私扫描（14 类模式）在干净版上 **全部 0 命中**：真实 userNid、两代 agent nid、真实姓名、
公网/内网 IP、本机用户名目录、旧口令、主机别名、学校机构名、JWT 形态、手机号、邮箱。

---

## 7. 一句话总评

**没有可被远程利用的漏洞，发布前必须修的隐私问题已全部修掉**；剩下最值得做的一件事是
把 journal / 日志里的账号 uid 换成短哈希（见 §2.1 第一条），其余的属于部署前提而非代码缺陷。

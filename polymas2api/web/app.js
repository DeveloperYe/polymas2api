/* polymas2api panel — plain ES2017, no inline script, no external dependency. */
(function () {
  "use strict";

  var KEY = "polymas2api.key";
  var state = { key: "", tab: "overview", timer: null, busy: false, modelAge: -1 };
  // Filled from the platform catalogue so the model picker can offer
  // every model the platform is able to route to.
  var MODEL_OPTIONS = ["Auto"];

  function $(id) { return document.getElementById(id); }
  function el(tag, attrs, children) {
    var node = document.createElement(tag);
    if (attrs) {
      Object.keys(attrs).forEach(function (name) {
        if (name === "class") node.className = attrs[name];
        else if (name === "text") node.textContent = attrs[name];
        else if (name.slice(0, 2) === "on") node.addEventListener(name.slice(2), attrs[name]);
        else if (attrs[name] !== null && attrs[name] !== undefined) node.setAttribute(name, attrs[name]);
      });
    }
    (children || []).forEach(function (child) {
      if (child === null || child === undefined) return;
      node.appendChild(typeof child === "string" ? document.createTextNode(child) : child);
    });
    return node;
  }
  function clear(node) { while (node && node.firstChild) node.removeChild(node.firstChild); }
  function fillBody(tableId, rows, emptyText) {
    var tbody = $(tableId).querySelector("tbody");
    clear(tbody);
    if (!rows || !rows.length) {
      tbody.appendChild(el("tr", {}, [el("td", { colspan: "12", class: "empty", text: emptyText })]));
      return;
    }
    rows.forEach(function (row) { tbody.appendChild(row); });
  }

  function authHeaders() {
    var headers = { "Content-Type": "application/json" };
    if (state.key) headers.Authorization = "Bearer " + state.key;
    return headers;
  }

  function api(path, options) {
    options = options || {};
    // Without a key the panel has nothing to show; asking anyway only
    // produces a row of 401s in the console.
    if (!state.key) {
      return Promise.reject(new Error("请先在上方填入口令并点「连接」"));
    }
    return fetch("/panel/api/" + path, {
      method: options.method || "GET",
      headers: authHeaders(),
      body: options.body ? JSON.stringify(options.body) : undefined
    }).then(function (response) {
      return response.json().then(function (payload) {
        if (!response.ok) {
          throw new Error(payload && payload.error ? payload.error.message : "HTTP " + response.status);
        }
        return payload;
      });
    });
  }

  function saveKey() {
    state.key = $("key").value.trim();
    try { sessionStorage.setItem(KEY, state.key); } catch (e) { /* private mode */ }
    if (state.key) {
      $("gate").classList.remove("on");
      loadAll();
    } else {
      $("gate").classList.add("on");
    }
  }

  function fmtTime(epoch) {
    if (!epoch) return "—";
    var d = new Date(epoch * 1000);
    var pad = function (n) { return n < 10 ? "0" + n : "" + n; };
    return (d.getMonth() + 1) + "-" + pad(d.getDate()) + " " +
           pad(d.getHours()) + ":" + pad(d.getMinutes()) + ":" + pad(d.getSeconds());
  }

  function fmtDuration(ms) {
    if (ms === null || ms === undefined) return "—";
    return ms < 1000 ? ms + " ms" : (ms / 1000).toFixed(2) + " s";
  }

  function fmtCount(value) {
    return Number(value || 0).toLocaleString("en-US");
  }

  /* ---- theme ---------------------------------------------------------- */
  function initTheme() {
    var button = $("theme-toggle");
    function paintIcon(mode) {
      var icon = $("theme-icon");
      if (!icon) return;
      // Sun for a fixed light mode, moon for a fixed dark mode, a half
      // disc for "follow the system".
      if (mode === "light") {
        icon.innerHTML = '<circle cx="8" cy="8" r="3.2"/><path d="M8 1.4v1.4M8 13.2v1.4M14.6 8h-1.4M2.8 8H1.4M12.7 3.3l-1 1M4.3 11.7l-1 1M12.7 12.7l-1-1M4.3 4.3l-1-1"/>';
      } else if (mode === "dark") {
        icon.innerHTML = '<path d="M13.4 9.6A5.6 5.6 0 0 1 6.4 2.6a5.8 5.8 0 1 0 7 7z"/>';
      } else {
        icon.innerHTML = '<circle cx="8" cy="8" r="5.4"/><path d="M8 2.6a5.4 5.4 0 0 1 0 10.8z" fill="currentColor" stroke="none"/>';
      }
    }
    paintIcon(window.polymasTheme.get());
    button.addEventListener("click", function () {
      var next = window.polymasTheme.cycle();
      paintIcon(next);
    });
  }

  function renderRecent(entries) {
    var rows = (entries || []).slice(0, 6).map(function (entry) {
      return el("tr", {}, [
        el("td", { class: "mono small nowrap", text: entry.time }),
        el("td", { class: "small muted nowrap", text: entry.model }),
        el("td", { class: "num", text: fmtDuration(entry.duration_ms) }),
        el("td", {}, [
          entry.ok
            ? el("span", { class: "badge on", text: "成功" })
            : el("span", { class: "badge bad", text: "失败" })
        ]),
        // The agent has a shell, so its tool use is worth seeing.
        el("td", { class: "small nowrap" }, [
          entry.tools
            ? el("span", { class: "badge info plain", text: entry.tools })
            : el("span", { class: "muted small", text: "—" })
        ])
      ]);
    });
    fillBody("recent", rows, "还没有请求");
    $("recent-sub").textContent = (entries || []).length + " 条";
  }

  /* ---- overview ------------------------------------------------------ */
  var METRICS = [
    { key: "total", label: "总请求" },
    { key: "ok", label: "成功", tone: "good" },
    { key: "failed", label: "失败", tone: "bad" },
    { key: "accounts", label: "账号数" },
    { key: "quarantined", label: "隔离中" },
    { key: "duration", label: "平均耗时", unit: "ms" },
    // Estimated: the platform reports no usage at all (see polymas2api/tokens.py),
    // so these are marked 估算 rather than passed off as measurements.
    { key: "in_tokens", label: "输入 token（估算）" },
    { key: "out_tokens", label: "输出 token（估算）" }
  ];

  function renderMetrics(data) {
    var stats = data.stats || {};
    var pool = data.pool || {};
    var values = {
      total: stats.total || 0,
      ok: stats.ok || 0,
      failed: stats.failed || 0,
      accounts: pool.total || 0,
      quarantined: pool.quarantined || 0,
      duration: stats.total ? Math.round(stats.total_duration_ms / stats.total) : 0,
      in_tokens: stats.prompt_tokens || 0,
      out_tokens: stats.completion_tokens || 0
    };
    var box = $("metrics");
    clear(box);
    METRICS.forEach(function (spec) {
      var value = values[spec.key];
      var node = el("div", { class: "metric" + (spec.tone ? " " + spec.tone : "") }, [
        el("div", { class: "label", text: spec.label }),
        el("div", { class: "value" })
      ]);
      var valueNode = node.querySelector(".value");
      if (spec.unit === "ms") {
        valueNode.appendChild(document.createTextNode(fmtDuration(value)));
      } else {
        valueNode.appendChild(document.createTextNode(fmtCount(value)));
      }
      box.appendChild(node);
    });
  }

  function renderFingerprint(info) {
    var box = $("fingerprint");
    clear(box);
    var rows = [
      ["客户端画像", info.profile || "—"],
      ["发送 Origin", info.send_origin ? "是" : "否"],
      ["TLS", info.tls || "—"],
      ["最小间隔", (info.min_interval_ms || 0) + " ms"],
      ["随机抖动", (info.jitter_ms || 0) + " ms"],
      ["思考延时", info.think_time_per_char_ms + " ms/字符（上限 " + info.think_time_cap_ms + " ms）"],
      ["请求体净化", info.sanitize_scope + (info.sanitize_enabled ? "" : "（已关闭）")],
      ["账号轮换", info.pool_enabled ? "开启" : "关闭"]
    ];
    rows.forEach(function (pair) {
      box.appendChild(el("dt", { text: pair[0] }));
      box.appendChild(el("dd", { text: String(pair[1]) }));
    });
  }

  function renderModels(data) {
    if (typeof data.models_age_seconds === "number") {
      state.modelAge = data.models_age_seconds;
    }
    var agent = (data && data.agent_rows) || [];
    var available = (data && data.available) || [];
    var rows = [];

    // The picker offers every model the platform lists, by modelCode (that is
    // what the upstream honours). The agent's own row is marked.
    var picker = ["Auto"];
    available.forEach(function (m) {
      if (m.modelCode && picker.indexOf(m.modelCode) < 0) picker.push(m.modelCode);
    });
    MODEL_OPTIONS = picker;

    (agent.length ? agent : available).forEach(function (model) {
      var isAgent = agent.some(function (a) { return a.modelCode === model.modelCode; });
      rows.push(el("tr", {}, [
        el("td", { class: "mono nowrap", text: model.modelCode || "—" }),
        el("td", { text: model.modelName || model.alias || "—" }),
        el("td", { class: "mono small muted", text: model.reasoningEffort || "—" }),
        el("td", {}, [
          isAgent
            ? el("span", { class: "badge info plain", text: "agent 默认" })
            : el("span", { class: "badge plain", text: "可选" })
        ]),
        el("td", { class: "num" }, [
          model.default ? el("span", { class: "badge on", text: "默认" })
                        : el("span", { class: "muted small", text: "—" })
        ])
      ]));
    });

    fillBody("models", rows, "没有模型");
    var age = state.modelAge;
    var ageText = "";
    if (typeof age === "number" && age >= 0) {
      if (age < 60) ageText = " · " + age + " 秒前获取";
      else if (age < 3600) ageText = " · " + Math.round(age / 60) + " 分钟前获取";
      else ageText = " · " + Math.round(age / 360) / 10 + " 小时前获取";
    }
    $("model-sub").textContent = "agent 指定 " + agent.length +
      " · 平台可用 " + available.length + ageText;

    var note = $("model-note");
    if (available.length > 1) {
      note.textContent = "可在「设置 → 模型」里指定其它模型，上游认这个字段（实测会真的换模型）。";
    } else {
      note.textContent = "";
    }
  }

  function renderAccountsMini(accounts) {
    var rows = (accounts || []).map(function (account) {
      return el("tr", {}, [
        el("td", { class: "mono nowrap", text: account.uid }),

        el("td", { class: "num", text: String(account.calls) }),
        el("td", { class: "num", text: String(account.failures) }),
        el("td", {}, [statusBadge(account)])
      ]);
    });
    fillBody("accounts-mini", rows, "没有账号");
  }

  /* The label is a note the operator writes; blank is a valid state and the
     panel shows "—" rather than echoing the uid from the neighbouring column. */
  function labelCell(account) {
    var input = el("input", {
      type: "text", value: account.label || "", maxlength: "60",
      class: "label-input", placeholder: "—",
      title: "备注名，改完回车保存"
    });
    input.addEventListener("change", function () {
      var value = (input.value || "").trim();
      if (value === (account.label || "")) return;
      api("accounts/" + encodeURIComponent(account.uid) + "/label", {
        method: "POST", body: { label: value }
      }).then(function () { loadAccounts(); })
        .catch(function (error) { showError(error); input.value = account.label || ""; });
    });
    return input;
  }

  /* Weight is a share, not a rate: 3:1 means the first account answers three
     quarters of the requests, interleaved rather than three in a row. */
  function weightCell(account) {
    var input = el("input", {
      type: "number", min: "1", max: "100", value: String(account.weight),
      class: "weight-input", title: "改完回车即可，权重决定分到的请求比例"
    });
    input.addEventListener("change", function () {
      var value = Number(input.value || 1);
      if (!(value >= 1 && value <= 100)) { input.value = String(account.weight); return; }
      api("accounts/" + encodeURIComponent(account.uid) + "/weight", {
        method: "POST", body: { weight: value }
      }).then(function () { loadAccounts(); loadOverview(); })
        .catch(function (error) { showError(error); input.value = String(account.weight); });
    });
    var share = account.share;
    var label = (typeof share === "number")
      ? el("span", { class: "small muted share", text: Math.round(share * 100) + "%" })
      : null;
    return el("span", { class: "weight-wrap" }, label ? [input, label] : [input]);
  }

  /* An ai-poly has no expiry claim, so the only honest per-account states are
     "有/没有" and "被上游拒过". */
  function credentialBadge(account) {
    if (!account.has_ai_poly) return el("span", { class: "badge off", text: "缺失" });
    if (account.last_error) {
      return el("span", { class: "badge bad", text: "曾被拒", title: account.last_error });
    }
    return el("span", { class: "badge on", text: "有效" });
  }

  function statusBadge(account) {
    if (account.last_error) return el("span", { class: "badge bad", text: "异常" });
    if (!account.enabled) return el("span", { class: "badge off", text: "停用" });
    return el("span", { class: "badge on", text: "启用" });
  }

  /* The gateway is normally served from the site root, but a reverse proxy may
     mount it under a prefix (e.g. https://host/gw/panel/). Deriving the prefix
     from the current path keeps the URL copy-pasteable in both cases — the
     old code used location.origin, which silently dropped /gw and produced a
     URL that could not be reached (found 2026-11-18). */
  function baseUrl() {
    var path = window.location.pathname || "/";
    var index = path.indexOf("/panel");
    var prefix = index > 0 ? path.slice(0, index).replace(/\/+$/, "") : "";
    return window.location.origin + prefix + "/v1";
  }

  function renderEndpoint(fingerprint) {
    var box = $("endpoint");
    clear(box);
    var base = baseUrl();
    var agent = (window.__panelModels || []).map(function (m) { return m.id; });
    var rows = [
      ["Base URL", base],
      ["实际模型", fingerprint.model || "—"],
      ["可切换模型", agent.length ? agent.join(", ") : "—"],
      ["助手 app_code", fingerprint.app_code || "—"],
      ["对话接口", "POST /v1/chat/completions"],
      ["模型列表", "GET /v1/models"],
      ["鉴权", "Authorization: Bearer <api_key>"]
    ];
    rows.forEach(function (pair) {
      box.appendChild(el("dt", { text: pair[0] }));
      var value = el("dd", { class: pair[0] === "Base URL" ? "mono" : "" });
      value.appendChild(document.createTextNode(String(pair[1])));
      box.appendChild(value);
    });
  }

  /* ---- credentials ---------------------------------------------------- */
  var CREDENTIAL_STATES = {
    ok: { label: "正常", cls: "on" },
    expiring: { label: "即将过期", cls: "warn" },
    expired: { label: "已过期", cls: "bad" },
    rejected: { label: "被平台拒绝", cls: "bad" },
    opaque: { label: "平台自行判定", cls: "off" }
  };

  /* One row per pooled account: each has its own ai-poly and renews on its
     own, so the table is about accounts, not about a single credential. */
  function renderCredentials(accounts, renewal) {
    var rows = (accounts || []).map(function (account) {
      var hasCred = !!account.has_ai_poly;
      var failed = !!account.renew_error;
      var badge = !hasCred
        ? el("span", { class: "badge off", text: "缺失" })
        : (failed
            ? el("span", { class: "badge bad", text: "续期失败" })
            : el("span", { class: "badge on", text: "有效" }));
      return el("tr", {}, [
        el("td", { class: "mono nowrap", text: account.uid }),
        el("td", { class: "small", text: hasCred ? "ai-poly" : "—" }),
        el("td", {}, [badge]),
        el("td", { class: "mono small nowrap", text: fmtTime(account.renewed_at) }),
        el("td", { class: "num",
                   text: (account.renew_count || account.renewed_at)
                     ? String(account.renew_count || 0) : "—" }),
        el("td", { class: "small", text: account.renew_error || "—" })
      ]);
    });
    fillBody("credentials", rows, "没有账号");

    var summary = $("renew-summary");
    var info = $("renew-info");
    if (!renewal) { summary.textContent = ""; info.textContent = ""; return; }
    summary.textContent = renewal.enabled
      ? ("每 " + renewal.interval_minutes + " 分钟") : "已关闭";
    var parts = [];
    if (renewal.renewed_at_local) parts.push("上次 " + renewal.renewed_at_local);
    if (renewal.failures) parts.push("失败 " + renewal.failures + " 次");
    if (renewal.last_error) parts.push(renewal.last_error);
    info.textContent = parts.length ? parts.join(" · ") : "本进程还没有续期记录";
  }

  /* ---- accounts ------------------------------------------------------- */
  function renderAccounts(data) {
    var rows = (data.accounts || []).map(function (account) {
      return el("tr", {}, [
        el("td", { class: "mono nowrap", text: account.uid }),
        el("td", {}, [labelCell(account)]),

        el("td", {}, [credentialBadge(account)]),
        el("td", { class: "num nowrap" }, [weightCell(account)]),
        el("td", { class: "num", text: String(account.calls) }),
        el("td", { class: "num", text: String(account.failures) }),
        el("td", { class: "mono small nowrap", text: fmtTime(account.last_used) }),
        el("td", {}, [statusBadge(account)]),
        el("td", { class: "num nowrap" }, [
          el("button", {
            class: "ghost small",
            text: account.enabled ? "停用" : "启用",
            onclick: function () {
              api("accounts/" + encodeURIComponent(account.uid) + "/enabled", {
                method: "POST", body: { enabled: !account.enabled }
              }).then(loadAccounts).catch(showError);
            }
          }),
          el("button", {
            class: "ghost small",
            text: "删除",
            onclick: function () {
              if (!window.confirm("删除账号 " + account.uid + "？")) return;
              api("accounts/" + encodeURIComponent(account.uid) + "/delete", {
                method: "POST", body: {}
              }).then(function () { loadAccounts(); loadAll(); }).catch(showError);
            }
          })
        ])
      ]);
    });
    fillBody("accounts", rows,
             "还没有账号 —— 点右上角「＋ 添加账号」，贴一串 ai-poly 即可");
    var stats = data.pool || {};
    $("pool-stats").textContent =
      "共 " + (stats.total || 0) + " · 启用 " + (stats.enabled || 0) + " · 隔离 " + (stats.quarantined || 0);
    $("pool-summary").textContent = (stats.enabled || 0) + " / " + (stats.total || 0) + " 启用";
  }

  /* ---- add account ---------------------------------------------------- */
  /* The form lives in a dialog: paste one ai-poly, it verifies itself, and
     确认添加 only lights up once the upstream has actually accepted it. */

  var verifyTimer = null;
  var verifiedAccount = null;

  function setVerifyState(text, kind) {
    var node = $("verify-state");
    node.textContent = text || "";
    node.className = "verify-state" + (kind ? " " + kind : "");
  }

  function accountPayload() {
    return { ai_poly: ($("new-ai-poly").value || "").trim() };
  }

  function openAddAccount() {
    verifiedAccount = null;
    $("new-ai-poly").value = "";
    $("confirm-add-account").disabled = true;
    setVerifyState("", "");
    var dialog = $("add-account-dialog");
    if (typeof dialog.showModal === "function") dialog.showModal();
    else dialog.setAttribute("open", "");
    $("new-ai-poly").focus();
  }

  function closeAddAccount() {
    clearTimeout(verifyTimer);
    var dialog = $("add-account-dialog");
    if (typeof dialog.close === "function") dialog.close();
    else dialog.removeAttribute("open");
  }

  function scheduleVerify() {
    clearTimeout(verifyTimer);
    verifiedAccount = null;
    $("confirm-add-account").disabled = true;
    var value = ($("new-ai-poly").value || "").trim();
    if (!value) { setVerifyState("", ""); return; }
    // A JWT has two dots; waiting for the third stops the panel from firing a
    // verification every time someone pastes half a cookie.
    if (value.split(".").length < 3) {
      setVerifyState("看起来不是完整的 ai-poly（JWT 应该有两段点）", "warn");
      return;
    }
    setVerifyState("校验中…", "muted");
    verifyTimer = setTimeout(runVerify, 500);
  }

  function runVerify() {
    if (($("new-ai-poly").value || "").trim() === "") return;
    setVerifyState("校验中…", "muted");
    api("accounts/verify", { method: "POST", body: accountPayload() })
      .then(function (result) {
        verifiedAccount = result.account;
        setVerifyState("凭据有效 · " + (result.account.user_nid || "") +
                       "，确认后会自动新建通用助手", "ok");
        $("confirm-add-account").disabled = false;
      })
      .catch(function (error) {
        verifiedAccount = null;
        $("confirm-add-account").disabled = true;
        setVerifyState(String(error && error.message ? error.message : error), "err");
      });
  }

  function confirmAddAccount() {
    var button = $("confirm-add-account");
    button.disabled = true;
    setVerifyState("正在新建通用助手…", "muted");
    // create_agent is the default on the server; verify stays on so the new
    // agent's model catalogue is pulled before anything is written to disk.
    api("accounts", { method: "POST", body: accountPayload() })
      .then(function (result) {
        var nid = (result.account && result.account.friend_nid) || "";
        closeAddAccount();
        loadAccounts();
        loadAll();
        if (nid) {
          var state = $("pool-stats");
          if (state) state.textContent = state.textContent + " · 新助手 " + nid;
        }
      })
      .catch(function (error) {
        setVerifyState(String(error && error.message ? error.message : error), "err");
        button.disabled = false;
      });
  }

/* ---- journal -------------------------------------------------------- */
  function renderJournal(data) {
    var rows = (data.entries || []).map(function (entry) {
      return el("tr", {}, [
        el("td", { class: "mono small nowrap", text: entry.time }),
        el("td", { class: "mono small nowrap", text: entry.account }),
        el("td", { class: "small muted nowrap", text: entry.model }),
        el("td", { class: "small muted", text: entry.stream ? "流式" : "普通" }),
        el("td", { class: "num", text: fmtDuration(entry.duration_ms) }),
        el("td", { class: "num", text: String(entry.prompt_chars) }),
        el("td", { class: "num", text: String(entry.content_chars) }),
        el("td", { class: "num", text: String(entry.prompt_tokens || 0) }),
        el("td", { class: "num", text: String(entry.completion_tokens || 0) }),
        el("td", {}, [
          entry.aborted
            ? el("span", { class: "badge warn", text: "已中断" })
            : (entry.ok
                ? el("span", { class: "badge on", text: "成功" })
                : el("span", { class: "badge bad", text: (entry.error || entry.status || "失败") }))
        ])
      ]);
    });
    fillBody("journal", rows, "还没有请求");
    var stats = data.stats || {};
    $("journal-stats").textContent =
      "共 " + (stats.total || 0) + " · 成功 " + (stats.ok || 0) +
      " · 失败 " + (stats.failed || 0) +
      " · token 估算 入 " + fmtCount(stats.prompt_tokens || 0) +
      " / 出 " + fmtCount(stats.completion_tokens || 0) +
      (stats.aborted ? " · 已中断 " + stats.aborted : "");
  }

  /* ---- errors --------------------------------------------------------- */
  function renderErrors(data) {
    var rows = (data.errors || []).map(function (row) {
      return el("tr", {}, [
        el("td", { class: "col-grow", text: row.error }),
        el("td", { class: "num", text: String(row.count) }),
        el("td", { class: "mono small nowrap", text: row.last_time }),
        el("td", { class: "mono small nowrap", text: row.account || "—" })
      ]);
    });
    fillBody("errors", rows, "没有错误");
    $("errors-stats").textContent = (data.errors || []).length + " 类";
  }

  /* ---- settings ------------------------------------------------------- */
  var SETTING_GROUPS = [
    {
      title: "模型",
      hint: "上游认这个字段，切了会真的换模型",
      items: [
        { key: "model", type: "model", label: "默认模型",
          hint: "Auto = agent 自带的那个；也可以指定平台目录里的任意模型" }
      ]
    },
    {
      title: "助手行为",
      hint: "这些开关上游认，实测有效",
      items: [
        { key: "use_agent", type: "switch", label: "启用助手能力",
          hint: "对应上游 configMsg.useAgent。关掉＝跳过 agent 直接裸调模型"
            + "（不查知识库、不做课程操作、不走 agent 人设）。课代表时代差别明显；"
            + "换成自建助手后实测已无可观测差异，建议保持开启" },
        { key: "web_retrieve", type: "switch", label: "允许联网检索",
          hint: "对应上游 configMsg.webRetrieve，本意是关掉后只查课程知识库。"
            + "但网关提示已把联网统一交给 harness 的 web_search/web_fetch，"
            + "开关关掉后仍走同一条路（实测 3.2s vs 3.5s，都发 CALL web_search），"
            + "已无实际差别，保持开启即可" },
        { key: "local_image_attachments", type: "switch", label: "图片落盘并告知路径",
          hint: "本网关不提供图片理解：上游只收纯文本，模型看不到图片内容。开启后把图片"
            + "存到本机临时目录并在提示词里写明路径，同时如实告知模型它看不到内容"
            + "（便于调用方自己的工具接手）；关掉则直接丢弃，只在提示词里留一句说明" },
        { key: "retain_long_term_memory", type: "switch", label: "写入长期记忆",
          hint: "开启时对话正文会进平台侧长期记忆，实测换 sessionId 也能被下一会话叫出来；"
            + "关掉后只保留本次会话上下文" },
        { key: "reasoning_effort", type: "select",
          options: ["(不发送)", "none", "minimal", "low", "medium", "high", "max"],
          label: "思考档位",
          hint: "按模型支持的档位选 —— 支持范围见总览页「模型」表的「档位」列，"
            + "每个模型不一样。none = 关闭思考通道，不发送 = 交给平台默认；"
            + "模型没声明的值平台不会报错，但效果不可预测；"
            + "请求里带 reasoning_effort 时以请求为准" }
      ]
    },
    {
      title: "工具调用",
      hint: "上游不接受工具定义，所以在提示词里描述、再把回答解析回来",
      items: [
        { key: "tool_bridge", type: "switch", label: "启用工具桥接",
          hint: "客户端传 tools 时，把工具描述写进提示词，并把 CALL 行解析成 tool_calls" },
        { key: "expose_upstream_tools", type: "switch", label: "上报上游自有工具",
          hint: "把 agent 自己的工具调用（TOOL_REQUEST/RESPONSE）放进 polymas.upstream_tools" },
        { key: "session_isolation", type: "switch", label: "请求间隔离上下文",
          hint: "不传 session_id 时每次请求用独立会话，避免互相串味" },
        { key: "tool_schema_depth", type: "number", label: "工具参数嵌套层数",
          hint: "0 = 只给顶层参数名与类型；2 = 展开到第二层（实测 112 个工具时 +7% 体积即完整）" },
        { key: "tool_name_style", type: "select", options: ["verbatim", "ascii"],
          label: "工具名风格",
          hint: "verbatim = 照抄客户端给的名字；ascii = 中文名换成英文（执行命令 → run_command）" },
        { key: "tool_retry", type: "switch", label: "没调工具就重试",
          hint: "它常回答「这就发给你」却不真的调用；重试一次能到 4/4。关掉更省时但不可靠" },
        { key: "tool_stream_buffer_chars", type: "number", label: "流式缓冲上限（字符）",
          hint: "答案短于此值时缓冲到结束再发，才能事后重试；更长则照常流式" }
      ]
    },
    {
      title: "对话模式",
      hint: "对应上游的 chatMode / actMode / terminal",
      items: [
        { key: "chat_mode", type: "select", options: ["COMMON", "FAST"],
          label: "对话模式", hint: "COMMON 常规 / FAST 快速" },
        { key: "act_mode", type: "select", options: ["AUTO", "PLAN", "ASK", "GOAL"],
          label: "行为模式",
          hint: "AUTO 自动 / PLAN 规划 / ASK 追问 / GOAL 目标；请求里带 act_mode 时以请求为准" },
        { key: "terminal", type: "select", options: ["STUDENT", "TEACHER"],
          label: "身份", hint: "学生端 / 教师端" }
      ]
    },
    {
      title: "出站指纹",
      hint: "上游看到的请求形态",
      items: [
        { key: "fingerprint", type: "select", options: ["edge-157", "chrome-131", "chrome-131-noorigin"],
          label: "客户端画像", hint: "UA 与客户端提示头组合" },
        { key: "send_origin", type: "switch", label: "发送 Origin 头",
          hint: "与网页端一致，服务端会回显该值" }
      ]
    },
    {
      title: "请求节律",
      hint: "避免机器流量形状",
      items: [
        { key: "min_interval_ms", type: "number", label: "最小调用间隔 (ms)",
          hint: "两次上游调用之间的下限" },
        { key: "jitter_ms", type: "number", label: "随机抖动 (ms)",
          hint: "每次调用叠加的随机延时" },
        { key: "think_time_per_char_ms", type: "number", label: "思考延时 (ms/字符)",
          hint: "按输入长度模拟人工输入耗时" },
        { key: "think_time_cap_ms", type: "number", label: "思考延时上限 (ms)", hint: "" }
      ]
    },
    {
      title: "账号池",
      hint: "多账号时的轮转策略",
      items: [
        { key: "pool_enabled", type: "switch", label: "启用账号池轮换", hint: "按权重轮转" }
      ]
    },
    {
      title: "名字清洗",
      hint: "跳过上游配置，只改输出",
      items: [
        { key: "persona_scrub_names", type: "text", label: "抹掉的名字",
          hint: "上游会拿平台账号名直接称呼你；填在这里的称呼会被去掉（逗号分隔）。留空即不启用" }
      ]
    },
    {
      title: "内容与日志",
      hint: "脱敏与留痕",
      items: [
        { key: "sanitize_scope", type: "select", options: ["off", "identity", "all"],
          label: "请求体净化范围", hint: "polymas 实测不按 system 逐字拦截，默认 off" },
        { key: "sanitize_enabled", type: "switch", label: "启用请求体净化", hint: "同上，非必需" },
        { key: "archive_enabled", type: "switch", label: "请求归档落盘", hint: "只写元数据，不写正文" },
        { key: "journal_size", type: "number", label: "日志保留条数", hint: "内存环形缓冲上限" }
      ]
    }
  ];

  var SETTINGS = [];
  SETTING_GROUPS.forEach(function (group) {
    group.items.forEach(function (item) { SETTINGS.push(item); });
  });

  function renderSettings(data) {
    var box = $("settings");
    clear(box);
    var values = data.settings || {};
    if (data.model_options && data.model_options.length) {
      MODEL_OPTIONS = data.model_options;
    }
    SETTING_GROUPS.forEach(function (group) {
      var groupBox = el("div", { class: "settings-group" }, [
        el("div", { class: "group-title", text: group.title }),
        el("div", { class: "group-hint", text: group.hint })
      ]);
      var fields = el("div", { class: "group-fields" });
      group.items.forEach(function (spec) {
        var id = "set-" + spec.key;
        var input;
        if (spec.type === "switch") {
          input = el("input", { type: "checkbox", id: id });
          input.checked = !!values[spec.key];
          fields.appendChild(el("div", { class: "field check" }, [
            el("span", { class: "switch" }, [input, el("span", { class: "track" })]),
            el("div", { class: "field-text" }, [
              el("label", { for: id, text: spec.label }),
              spec.hint ? el("div", { class: "hint", text: spec.hint }) : null
            ])
          ]));
          return;
        }
        if (spec.type === "select") {
          input = el("select", { id: id });
          spec.options.forEach(function (option) {
            var node = el("option", { value: option, text: option });
            if (values[spec.key] === option) node.selected = true;
            input.appendChild(node);
          });
          // "(不发送)" stands for an empty value.
          if (values[spec.key] === "" || values[spec.key] === undefined) {
            input.value = spec.options[0];
          }
        } else if (spec.type === "model") {
          input = el("select", { id: id });
          var list = MODEL_OPTIONS || ["Auto"];
          list.forEach(function (option) {
            var node = el("option", { value: option, text: option });
            if (values[spec.key] === option) node.selected = true;
            input.appendChild(node);
          });
        } else if (spec.type === "text") {
          input = el("input", { type: "text", id: id, value: values[spec.key] || "" });
        } else {
          input = el("input", { type: "number", id: id,
            value: values[spec.key] === undefined || values[spec.key] === null ? 0 : values[spec.key] });
        }
        fields.appendChild(el("div", { class: "field" }, [
          el("label", { for: id, text: spec.label }),
          input,
          spec.hint ? el("div", { class: "hint", text: spec.hint }) : null
        ]));
      });
      groupBox.appendChild(fields);
      box.appendChild(groupBox);
    });
  }

  function collectSettings() {
    var payload = {};
    SETTINGS.forEach(function (spec) {
      var node = $("set-" + spec.key);
      if (!node) return;
      if (spec.type === "switch") payload[spec.key] = node.checked;
      else if (spec.type === "number") payload[spec.key] = Number(node.value);
      else if (spec.type === "select" && node.value === "(不发送)") payload[spec.key] = "";
      else payload[spec.key] = node.value;    });
    return payload;
  }

  /* ---- loaders -------------------------------------------------------- */
  function showError(error) {
    var box = $("check-output");
    if (box) box.textContent = String(error && error.message ? error.message : error);
    $("save-state").textContent = String(error && error.message ? error.message : error);
    $("save-state").className = "small err";
  }

  function loadOverview() {
    return api("overview").then(function (data) {
      renderMetrics(data);
      renderFingerprint(data.fingerprint || {});
      window.__panelModels = data.models || [];
      renderEndpoint(data.fingerprint || {});
      renderModels(data);
      renderAccountsMini((data.accounts) || []);
      renderRecent(data.recent || []);
      renderCredentials(data.accounts || [], data.renewal || {});
      // app_code is the configured agent; with several accounts each one has its
      // own agent and requests rotate, so say how many are in play.
      var poolNow = (data.agent && data.agent.pool) || {};
      var distinct = poolNow.distinct || 1;
      $("app-code").textContent = "app_code "
        + ((data.fingerprint && data.fingerprint.app_code) || "—")
        + (distinct > 1 ? " · " + distinct + " 个助手轮换" : "");
      var agentName = (data.agent && data.agent.name) || "";
      $("agent-name").textContent = agentName + (distinct > 1 ? " 等" : "");
    }).catch(showError);
  }
  function loadAccounts() {
    return api("accounts").then(renderAccounts).catch(showError);
  }
  function loadJournal() {
    return api("journal").then(renderJournal).catch(showError);
  }
  function loadErrors() {
    return api("errors").then(renderErrors).catch(showError);
  }
  function loadSettings() {
    return api("settings").then(renderSettings).catch(showError);
  }

  function loadAll() {
    var jobs = [loadOverview(), loadJournal(), loadErrors()];
    if (state.tab === "accounts") jobs.push(loadAccounts());
    if (state.tab === "settings") jobs.push(loadSettings());
    setBusy(true);
    return Promise.all(jobs).then(function () { setBusy(false); },
                            function () { setBusy(false); });
  }

  /* The 5 s poll must never rebuild the settings form. It used to, and the
     result was that picking a model — or flipping any switch — was wiped
     within five seconds, before 保存 could be pressed. Measured 2026-11-18:
     the model picker flashed and fell back to "Auto" every single time.
     Settings only change through 保存, so the poll has nothing to refresh. */
  function refreshLive() {
    var jobs = [loadOverview(), loadJournal(), loadErrors()];
    if (state.tab === "accounts") jobs.push(loadAccounts());
    setBusy(true);
    return Promise.all(jobs).then(function () { setBusy(false); },
                            function () { setBusy(false); });
  }

  function switchTab(name) {
    state.tab = name;
    Array.prototype.forEach.call(document.querySelectorAll(".tab"), function (tab) {
      tab.classList.toggle("active", tab.dataset.tab === name);
    });
    Array.prototype.forEach.call(document.querySelectorAll(".tab-panel"), function (panel) {
      panel.classList.toggle("active", panel.id === "tab-" + name);
    });
    loadAll();
  }

  function refreshHealth() {
    fetch("/health").then(function (r) { return r.json(); }).then(function (data) {
      $("status").className = "status live";
      $("status-text").textContent = "运行中";
      $("app-code").textContent = "app_code " + (data.app_code || "—");
    }).catch(function () {
      $("status").className = "status down";
      $("status-text").textContent = "无法连接";
    });
  }

  /* A pulse while a panel request is in flight: the panel refreshes every
   * few seconds, and a still screen looks broken. */
  function setBusy(busy) {
    state.busy = busy;
    var dot = $("status").querySelector(".dot");
    if (dot) dot.classList.toggle("pulse", busy);
  }

  document.addEventListener("DOMContentLoaded", function () {
    try { state.key = sessionStorage.getItem(KEY) || ""; } catch (e) { /* ignore */ }
    $("key").value = state.key;

    initTheme();

    Array.prototype.forEach.call(document.querySelectorAll(".tab"), function (tab) {
      tab.addEventListener("click", function () { switchTab(tab.dataset.tab); });
    });
    $("save-key").addEventListener("click", saveKey);
    $("key").addEventListener("keydown", function (event) {
      if (event.key === "Enter") saveKey();
    });
    $("refresh").addEventListener("click", loadAll);

    $("copy-base").addEventListener("click", function () {
      var text = baseUrl();
      var state = $("copy-state");
      function done(ok) {
        state.textContent = ok ? "已复制 " + text : "复制失败，请手动选取";
        state.className = "small " + (ok ? "ok" : "err");
        setTimeout(function () { state.textContent = ""; state.className = "muted small"; }, 2500);
      }
      if (navigator.clipboard && navigator.clipboard.writeText) {
        navigator.clipboard.writeText(text).then(function () { done(true); }, function () { done(false); });
      } else {
        done(false);
      }
    });

    $("refresh-models").addEventListener("click", function () {
      var button = $("refresh-models");
      button.disabled = true;
      button.textContent = "获取中…";
      api("models/refresh", { method: "POST", body: {} })
        .then(function (result) {
          renderModels(result);
          if (result.ok) loadOverview();
        })
        .catch(showError)
        .then(function () {
          button.disabled = false;
          button.textContent = "重新获取";
        });
    });

    $("open-add-account").addEventListener("click", openAddAccount);
    $("close-add-account").addEventListener("click", closeAddAccount);
    $("cancel-add-account").addEventListener("click", closeAddAccount);
    $("confirm-add-account").addEventListener("click", confirmAddAccount);
    $("new-ai-poly").addEventListener("input", scheduleVerify);
    $("new-ai-poly").addEventListener("paste", function () { setTimeout(scheduleVerify, 0); });

    $("save-settings").addEventListener("click", function () {
      $("save-state").textContent = "保存中…";
      $("save-state").className = "small muted";
      api("settings", { method: "POST", body: collectSettings() })
        .then(function () {
          $("save-state").textContent = "已保存并生效";
          $("save-state").className = "small ok";
        })
        .catch(showError);
    });

    $("check-upstream").addEventListener("click", function () {
      $("check-output").textContent = "检查中…";
      api("check-upstream", { method: "POST" })
        .then(function (data) { $("check-output").textContent = JSON.stringify(data, null, 2); })
        .catch(showError);
    });

    $("renew-now").addEventListener("click", function () {
      var button = $("renew-now");
      button.disabled = true;
      button.textContent = "续期中…";
      api("renew", { method: "POST" })
        .then(function (data) {
          button.textContent = data.ok ? "续期成功" : "续期失败";
          loadOverview();
        })
        .catch(function (error) {
          button.textContent = "续期失败";
          showError(error);
        });
      setTimeout(function () {
        button.disabled = false;
        button.textContent = "立即续期";
      }, 3000);
    });

    $("apply-persona").addEventListener("click", function () {
      var button = $("apply-persona");
      var info = $("persona-info");
      button.disabled = true;
      button.textContent = "写入中…";
      api("persona/apply", { method: "POST", body: {} })
        .then(function (data) {
          var failures = (data.results || []).filter(function (item) {
            return !item.applied;
          });
          var failed = data.total - data.applied;
          info.textContent = failed
            ? "已写入 " + data.applied + "/" + data.total + "，失败：" +
              failures.map(function (item) {
                return item.uid + "（" + item.error + "）";
              }).join("；")
            : "已写入 " + data.applied + " 个助手";
        })
        .catch(function (error) {
          info.textContent = "写入失败";
          showError(error);
        })
        .then(function () {
          button.disabled = false;
          button.textContent = "写入助手人设";
        });
    });

    refreshHealth();
    if (state.key) {
      $("gate").classList.remove("on");
      loadAll();
    } else {
      $("gate").classList.add("on");
    }
    state.timer = setInterval(function () {
      if (document.visibilityState === "visible" && !state.busy && state.key) refreshLive();
    }, 5000);
  });
})();

/* qwenpaw-guard v0.0.1 AI 守护者看板 React 组件。
 * 仿照内置插件（file-browser / qwenpaw-team）范式注册：
 *   QP.registerRoutes -> App Center 页（/apps/qwenpaw-guard）
 *   QP.route.add       -> 页面路由（/plugin/qwenpaw-guard）
 *   QP.menu.add        -> 侧边栏菜单（primary.settings）
 * 组件内用唯一 id 前缀渲染骨架，避免多实例冲突。
 * 版本号与 plugin.json / constants.PLUGIN_VERSION 一致。
 */
(function () {
  "use strict";

  var QP = window.QwenPaw;
  if (!QP || !QP.host) {
    console.error("[qwenpaw-guard] QwenPaw not ready");
    return;
  }
  var React = QP.host.React;
  var h = React.createElement;

  var PLUGIN_VERSION = "0.0.1";
  var PLUGIN_ID = "qwenpaw-guard";
  var PLUGIN_NAME = "守护者";
  var API = "/api/qwenpaw-guard";

  var CSS = [
    ".qgd-root{--qgd-bg:#f4f7fc;--qgd-card:#fff;--qgd-line:#e7ecf5;--qgd-line2:#dfe6f2;--qgd-ink:#232f4b;--qgd-sub:#7d8aa0;--qgd-brand:#4f6ef2;--qgd-brand2:#6f8bff;--qgd-ok:#1f9d61;--qgd-warn:#c07a12;--qgd-err:#d64545;--qgd-info:#2f7dd1;font-family:-apple-system,BlinkMacSystemFont,'Segoe UI','PingFang SC','Hiragino Sans GB','Microsoft YaHei',sans-serif;color:var(--qgd-ink);}",
    ".qgd-root *{box-sizing:border-box;}",
    ".qgd-head{display:flex;align-items:center;justify-content:space-between;margin-bottom:14px;font-size:15px;flex-wrap:wrap;gap:8px;}",
    ".qgd-head h1{font-size:19px;margin:0;font-weight:800;color:var(--qgd-ink);}",
    ".qgd-reload{font-size:13px;color:var(--qgd-brand);cursor:pointer;user-select:none;border:1px solid var(--qgd-line2);border-radius:8px;padding:4px 12px;background:#fff;transition:all .15s ease;}",
    ".qgd-reload:hover{background:#eef2ff;border-color:var(--qgd-brand);}",
    ".qgd-card{background:var(--qgd-card);border:1px solid var(--qgd-line);border-radius:14px;padding:16px 20px;margin-bottom:14px;box-shadow:0 1px 2px rgba(24,39,75,.04),0 6px 18px -8px rgba(24,39,75,.08);}",
    ".qgd-card h2{font-size:15px;margin:2px 0 12px;font-weight:700;color:var(--qgd-ink);display:flex;align-items:center;gap:7px;}",
    ".qgd-card h2::after{content:'';flex:1;height:1px;background:linear-gradient(90deg,var(--qgd-line2),transparent);}",
    ".qgd-stats{display:grid;grid-template-columns:repeat(auto-fit,minmax(140px,1fr));gap:12px;}",
    ".qgd-stat{display:flex;align-items:center;gap:11px;padding:12px 14px;border-radius:12px;border:1px solid var(--qgd-line);background:linear-gradient(180deg,#fff,#f8fbff);}",
    ".qgd-stat-ic{width:40px;height:40px;border-radius:11px;display:flex;align-items:center;justify-content:center;font-size:21px;flex-shrink:0;}",
    ".qgd-stat-tx b{display:block;font-size:20px;font-weight:800;color:var(--qgd-ink);line-height:1.15;}",
    ".qgd-stat-tx span{font-size:12px;color:var(--qgd-sub);}",
    ".qgd-agent{border:1px solid var(--qgd-line);border-radius:12px;padding:10px 14px;margin-bottom:8px;background:linear-gradient(180deg,#fff,#fafcff);}",
    ".qgd-agent-head{display:flex;align-items:center;gap:8px;margin-bottom:6px;flex-wrap:wrap;}",
    ".qgd-agent-head b{font-size:14px;color:var(--qgd-ink);}",
    ".qgd-agent-head .tag{color:var(--qgd-sub);font-size:12px;}",
    ".qgd-sess{display:flex;align-items:center;gap:7px;padding:4px 8px;border-radius:7px;margin:3px 0;font-size:12.5px;color:#45537a;background:#f8fbff;border:1px solid #eef2fb;}",
    ".qgd-sess .dot{width:8px;height:8px;border-radius:50%;flex-shrink:0;}",
    ".qgd-sess .dot.run{background:#1faf68;box-shadow:0 0 0 3px rgba(31,175,104,.16);}",
    ".qgd-sess .dot.act{background:#f0a93a;}",
    ".qgd-sess .dot.idle{background:#c3cbdb;}",
    ".qgd-sess .sid{color:#9aa6bc;font-size:11px;overflow:hidden;text-overflow:ellipsis;white-space:nowrap;max-width:180px;}",
    ".qgd-table{width:100%;border-collapse:collapse;font-size:13px;}",
    ".qgd-table th{background:#f6f9fe;color:var(--qgd-sub);font-weight:600;padding:8px 10px;border-bottom:1px solid var(--qgd-line);text-align:left;white-space:nowrap;}",
    ".qgd-table td{padding:7px 10px;border-bottom:1px solid #f0f4fb;text-align:left;vertical-align:middle;}",
    ".qgd-btn{display:inline-block;padding:3px 14px;font-size:12px;border-radius:7px;cursor:pointer;border:1px solid transparent;transition:all .15s ease;margin-right:4px;}",
    ".qgd-btn.ok{color:#15834e;border-color:#b7e3c9;background:rgba(31,157,97,.07);}",
    ".qgd-btn.ok:hover{background:#1f9d61;color:#fff;border-color:#1f9d61;}",
    ".qgd-btn.no{color:#c93a3a;border-color:#f5c6c6;background:rgba(214,69,69,.05);}",
    ".qgd-btn.no:hover{background:#d64545;color:#fff;border-color:#d64545;}",
    ".qgd-empty{color:#9aa6bc;font-size:13px;padding:8px 0;}",
    ".qgd-form{display:flex;gap:8px;flex-wrap:wrap;align-items:flex-end;}",
    ".qgd-form label{display:block;font-size:12px;color:var(--qgd-sub);margin-bottom:3px;}",
    ".qgd-form input{width:240px;max-width:100%;padding:7px 10px;border:1px solid var(--qgd-line2);border-radius:8px;font-size:13px;}",
    ".qgd-form input:focus{outline:none;border-color:var(--qgd-brand);box-shadow:0 0 0 3px rgba(79,110,242,.14);}",
    ".qgd-form select{width:240px;max-width:100%;padding:7px 10px;border:1px solid var(--qgd-line2);border-radius:8px;font-size:13px;color:var(--qgd-ink);background:#fff;cursor:pointer;appearance:auto;}",
    ".qgd-form select:focus{outline:none;border-color:var(--qgd-brand);box-shadow:0 0 0 3px rgba(79,110,242,.14);}",
    ".qgd-form button{padding:7px 16px;background:linear-gradient(135deg,var(--qgd-brand),var(--qgd-brand2));color:#fff;border:none;border-radius:8px;cursor:pointer;font-size:13px;font-weight:600;}",
    ".qgd-msg{font-size:12px;color:var(--qgd-sub);min-height:15px;margin-top:6px;}",
    ".qgd-ver{font-size:12px;color:#a7b1c5;font-weight:normal;margin-left:6px;}",
    ".qgd-mono{font-family:ui-monospace,'SF Mono','Cascadia Code',Menlo,Consolas,monospace;font-size:12px;}",
    ".qgd-filter{display:inline-flex;gap:4px;margin-left:auto;}",
    ".qgd-filter .qgd-btn{background:#fff;color:var(--qgd-sub);border-color:var(--qgd-line2);margin-right:0;}",
    ".qgd-filter .qgd-btn.on{background:var(--qgd-brand);color:#fff;border-color:var(--qgd-brand);}",
    ".qgd-btn.guard{color:#2f7dd1;border-color:#bcd8f5;background:rgba(47,125,209,.07);}",
    ".qgd-btn.guard.on{background:#2f7dd1;color:#fff;border-color:#2f7dd1;}",
    ".qgd-btn.ghost{color:#6b778c;border-color:#e2e8f2;background:#fff;}",
    ".qgd-btn.ghost:hover{background:#f1f4fa;}",
    ".qgd-sess .act{display:inline-flex;align-items:center;gap:4px;margin-left:6px;flex-shrink:0;}",
    ".qgd-status-ok{color:var(--qgd-ok);font-size:12px;font-weight:600;}",
    ".qgd-status-no{color:var(--qgd-err);font-size:12px;font-weight:600;}",
    ".qgd-toolbar{display:flex;gap:6px;align-items:center;margin-bottom:10px;flex-wrap:wrap;}",
    ".qgd-toolbar .qgd-btn{background:#fff;}",
    ".qgd-archived{opacity:.62;background:#f4f6fa !important;border-color:#e6ebf4 !important;}",
    ".qgd-modal-mask{position:fixed;inset:0;background:rgba(16,26,52,.46);z-index:9999;display:flex;align-items:center;justify-content:center;backdrop-filter:blur(2px);}",
    ".qgd-modal{width:360px;max-width:92vw;background:#fff;border-radius:16px;box-shadow:0 24px 70px rgba(16,26,52,.36);padding:20px 22px;border:1px solid var(--qgd-line);}",
    ".qgd-modal h3{margin:0 0 10px;font-size:16px;font-weight:700;color:var(--qgd-ink);}",
    ".qgd-modal .qgd-modal-body{font-size:13.5px;color:#4a5978;line-height:1.7;margin-bottom:16px;white-space:pre-line;word-break:break-all;}",
    ".qgd-modal .qgd-modal-input{width:100%;padding:9px 11px;border:1px solid var(--qgd-line2);border-radius:9px;font-size:13.5px;margin-bottom:16px;color:var(--qgd-ink);}",
    ".qgd-modal .qgd-modal-input:focus{outline:none;border-color:var(--qgd-brand);box-shadow:0 0 0 3px rgba(79,110,242,.14);}",
    ".qgd-modal-foot{display:flex;justify-content:flex-end;gap:8px;}",
  ];
  var styleEl = document.getElementById("qgd-style");
  if (!styleEl) {
    styleEl = document.createElement("style");
    styleEl.id = "qgd-style";
    styleEl.textContent = CSS.join("\n");
    document.head.appendChild(styleEl);
  }

  function fmtTime(iso) {
    if (!iso) return "-";
    try { return new Date(iso).toLocaleString(); } catch (e) { return iso; }
  }

  function GuardBoardComponent() {
    // uid 必须跨渲染稳定：放 useRef，否则 useEffect 依赖每次渲染变化 → 无限刷新
    var uidRef = React.useRef(null);
    if (!uidRef.current) {
      uidRef.current = PLUGIN_ID + "-" + Math.random().toString(36).slice(2, 8);
    }
    var uid = uidRef.current;
    var S = React.useState;
    var [data, setData] = S(null);
    var [err, setErr] = S("");
    var [form, setForm] = S({ channel: "", target_user: "", target_session: "", feishu_topic_chat_id: "", send_mode: "reply" });
    var [msg, setMsg] = S("");
    var [auto, setAuto] = S(true);
    var [filter, setFilter] = S("all"); // collapse | recent | all
    var [showArchived, setShowArchived] = S(false);
    var [topics, setTopics] = S([]); // 自动检测到的话题群列表（>1 时展示下拉）
    var [modal, setModal] = S(null); // 自定义弹窗（替代浏览器默认 confirm/prompt）
    var promptInputRef = React.useRef(null); // prompt 输入框（onOk 读实时值，避免闭包过期）

    function load() {
      fetch(API + "/status")
        .then(function (r) { return r.json(); })
        .then(function (d) {
          setData(d);
          setErr("");
          if (!d.config) return;
          setForm(function (f) {
            return {
              channel: d.config.channel || "feishu",
              target_user: d.config.target_user || "",
              target_session: d.config.target_session || "",
              feishu_topic_chat_id: d.config.feishu_topic_chat_id || "",
              send_mode: d.config.send_mode || "reply",
            };
          });
        })
        .catch(function (e) { setErr("加载失败: " + String(e)); });
    }

    React.useEffect(function () {
      load();
      var timer = setInterval(function () {
        if (auto) load();
      }, 5000);
      return function () { clearInterval(timer); };
    }, [uid, auto]);

    // prompt 弹窗打开时聚焦输入框并全选，方便直接改名
    React.useEffect(function () {
      if (modal && modal.type === "prompt" && promptInputRef.current) {
        promptInputRef.current.focus();
        promptInputRef.current.select();
      }
    }, [modal]);

    function applyConfig(cfg, silent) {
      fetch(API + "/config", {
        method: "POST",
        headers: { "Content-Type": "application/json" },
        body: JSON.stringify(cfg),
      })
        .then(function (r) { return r.json(); })
        .then(function (d) {
          if (!silent) {
            setMsg(d.ok ? "已保存配置" : "保存失败: " + (d.error || ""));
            if (d.ok) load();
          }
        })
        .catch(function (e) { if (!silent) setMsg("保存失败: " + String(e)); });
    }

    function saveConfig() {
      applyConfig(form);
    }

    function discover() {
      fetch(API + "/discover")
        .then(function (r) { return r.json(); })
        .then(function (d) {
          var c = (d.chats || [])[0];
          if (!c) { setMsg("未发现飞书会话，请先确保配置了飞书频道"); return; }
          setForm(function (f) {
            return { channel: d.channel || f.channel, target_user: c.user_id, target_session: c.session_id };
          });
          setMsg("已发现飞书会话: " + c.session_id);
        })
        .catch(function (e) { setMsg("发现失败: " + String(e)); });
    }

    function discoverTopics() {
      setMsg("检测中…");
      fetch(API + "/discover-topics")
        .then(function (r) { return r.json(); })
        .then(function (d) {
          var list = d.topics || [];
          if (!list.length) {
            setTopics([]);
            setMsg("未检测到话题群。请先在飞书创建「话题群」并把机器人拉入群，再自动检测。");
            return;
          }
          setTopics(list);
          if (list.length === 1) {
            var t = list[0];
            var nf = Object.assign({}, form, { feishu_topic_chat_id: t.chat_id });
            setForm(nf);
            setMsg("已自动检测并保存话题群：" + (t.name || t.chat_id));
            applyConfig(nf);
          } else {
            setMsg("检测到 " + list.length + " 个话题群，请在下拉中选择后点「保存」。");
          }
        })
        .catch(function (e) { setMsg("自动检测失败: " + String(e)); });
    }

    function testSend() {
      fetch(API + "/test-send", {
        method: "POST",
        headers: { "Content-Type": "application/json" },
        body: JSON.stringify({}),
      })
        .then(function (r) { return r.json(); })
        .then(function (d) { setMsg(d.ok ? "测试消息已发送" : "发送失败: " + (d.message || "")); })
        .catch(function (e) { setMsg("发送失败: " + String(e)); });
    }

    function guardSession(a, s) {
      var isG = guardedSet[a.id + ":" + s.session_id];
      fetch(API + "/sessions/guard", {
        method: "POST",
        headers: { "Content-Type": "application/json" },
        body: JSON.stringify({
          agent_id: a.id,
          session_id: s.session_id,
          channel: s.channel,
          name: s.name,
          guarded: !isG,
        }),
      })
        .then(function (r) { return r.json(); })
        .then(function (d) {
          setMsg(d.ok ? (isG ? "已取消守护" : "已守护") : "失败: " + (d.error || ""));
          if (d.ok) load();
        })
        .catch(function (e) { setMsg("失败: " + String(e)); });
    }

    function deleteSession(a, s) {
      setModal({
        type: "confirm",
        title: "删除会话",
        message: "确定删除该会话？\n将移除会话记录，并把会话状态文件移入回收站（可恢复）。",
        okText: "删除",
        cancelText: "取消",
        onOk: function () {
          setModal(null);
          fetch(API + "/sessions/delete", {
            method: "POST",
            headers: { "Content-Type": "application/json" },
            body: JSON.stringify({ agent_id: a.id, session_id: s.session_id }),
          })
            .then(function (r) { return r.json(); })
            .then(function (d) {
              setMsg(d.ok ? "已删除会话" : "失败: " + (d.error || ""));
              if (d.ok) load();
            })
            .catch(function (e) { setMsg("失败: " + String(e)); });
        },
      });
    }

    function archiveSession(a, s) {
      var isA = archivedSet[a.id + ":" + s.session_id];
      fetch(API + "/sessions/archive", {
        method: "POST",
        headers: { "Content-Type": "application/json" },
        body: JSON.stringify({
          agent_id: a.id,
          session_id: s.session_id,
          channel: s.channel,
          name: s.name,
          archived: !isA,
        }),
      })
        .then(function (r) { return r.json(); })
        .then(function (d) {
          setMsg(d.ok ? (isA ? "已恢复会话" : "已归档") : "失败: " + (d.error || ""));
          if (d.ok) load();
        })
        .catch(function (e) { setMsg("失败: " + String(e)); });
    }

    function renameSession(a, s) {
      setModal({
        type: "prompt",
        title: "重命名会话",
        initial: s.name || "",
        placeholder: "输入新的会话名称",
        okText: "保存",
        cancelText: "取消",
        onOk: function () {
          var name = (promptInputRef.current ? promptInputRef.current.value : "").trim();
          if (!name) { setMsg("名称不能为空"); return; }
          setModal(null);
          fetch(API + "/sessions/rename", {
            method: "POST",
            headers: { "Content-Type": "application/json" },
            body: JSON.stringify({ agent_id: a.id, session_id: s.session_id, name: name }),
          })
            .then(function (r) { return r.json(); })
            .then(function (d) {
              setMsg(d.ok ? "已重命名" : "失败: " + (d.error || ""));
              if (d.ok) load();
            })
            .catch(function (e) { setMsg("失败: " + String(e)); });
        },
      });
    }

    var agents = (data && data.agents) || [];

    // 守护会话集合：agent_id:session_id -> true
    var guardedSet = {};
    ((data && data.guarded) || []).forEach(function (g) {
      guardedSet[g.agent_id + ":" + g.session_id] = true;
    });
    // 归档会话集合：agent_id:session_id -> true
    var archivedSet = {};
    ((data && data.archived) || []).forEach(function (g) {
      archivedSet[g.agent_id + ":" + g.session_id] = true;
    });

    // 统计卡片：与上方会话列表同源（同一份 a.sessions），
    // 避免删除会话/筛选后计数漂移（顶部数字与列表不一致）。
    var allSessions = agents.reduce(function (acc, a) {
      return acc.concat((a.sessions || []).map(function (s) { s.agent_id = a.id; return s; }));
    }, []);
    var totalSessions = allSessions.length;
    var totalActive = allSessions.filter(function (s) { return s.active; }).length;
    // 守护数量：只统计「仍存在于会话列表中的守护会话」，与列表标记同一来源，
    // 避免已删除会话在 guarded.json 里的残留记录被误计入。
    var guardedCount = allSessions.filter(function (s) {
      return guardedSet[s.agent_id + ":" + s.session_id];
    }).length;
    var statRow = [
      stat("agents", "🤖", agents.length, "Agent"),
      stat("sessions", "💬", totalSessions, "会话"),
      stat("active", "🟢", totalActive, "活动会话"),
      stat("guarded", "🛡️", guardedCount, "守护会话"),
    ];

    // 三态筛选
    var filterLabels = [
      ["collapse", "折叠所有"],
      ["recent", "最近两天"],
      ["all", "全部展开"],
    ];
    var filterButtons = filterLabels.map(function (f) {
      return h("span", {
        className: "qgd-btn" + (filter === f[0] ? " on" : ""),
        key: f[0],
        onClick: function () { setFilter(f[0]); },
      }, f[1]);
    });

    function sessVisible(s) {
      if (s.archived && !showArchived) return false;
      if (filter === "collapse") return false;
      if (filter === "recent") return !!s.recent;
      return true;
    }

    var agentCards = agents.map(function (a) {
      var allS = a.sessions || [];
      var runN = allS.filter(function (s) { return s.running; }).length;
      var arcN = allS.filter(function (s) { return s.archived; }).length;
      var sessRows = allS.filter(sessVisible).map(function (s) {
        var cls = s.running ? "run" : (s.archived ? "idle" : (s.recent ? "act" : "idle"));
        var isG = guardedSet[a.id + ":" + s.session_id];
        var isA = archivedSet[a.id + ":" + s.session_id];
        return h("div", { className: "qgd-sess" + (isA ? " qgd-archived" : ""), key: s.id },
          h("span", { className: "dot " + cls }),
          h("span", {}, "[" + s.channel + "] " + (isA ? "📦 " : "") + s.name),
          h("span", { className: "sid" }, s.session_id),
          h("span", { style: { marginLeft: "auto", color: "#9aa6bc", fontSize: 11 } }, fmtTime(s.updated_at)),
          h("span", { className: "qgd-btn guard" + (isG ? " on" : ""), onClick: function () { guardSession(a, s); } }, isG ? "已守护" : "守护"),
          h("span", { className: "qgd-btn ghost" + (isA ? " on" : ""), onClick: function () { archiveSession(a, s); } }, isA ? "恢复" : "归档"),
          h("span", { className: "qgd-btn ghost", onClick: function () { renameSession(a, s); } }, "重命名"),
          h("span", { className: "qgd-btn ghost", onClick: function () { deleteSession(a, s); } }, "删除")
        );
      });
      return h("div", { className: "qgd-agent", key: a.id },
        h("div", { className: "qgd-agent-head" },
          h("b", {}, a.name + " (" + a.id + ")"),
          h("span", { className: "tag" }, "会话 " + allS.length + " · 运行 " + runN + (arcN ? " · 归档 " + arcN : "")),
          !a.enabled && h("span", { style: { color: "#d64545", fontSize: 12 } }, "【已停用】")
        ),
        sessRows.length ? sessRows : h("div", { className: "qgd-empty" }, filter === "collapse" ? "（已折叠）" : (showArchived ? "（无会话）" : "（无会话）"))
      );
    });

    function renderModal() {
      if (!modal) return null;
      var isPrompt = modal.type === "prompt";
      return h("div", { className: "qgd-modal-mask", onClick: function () { setModal(null); } },
        h("div", { className: "qgd-modal", onClick: function (e) { e.stopPropagation(); } },
          h("h3", {}, modal.title || (isPrompt ? "输入" : "确认操作")),
          modal.message !== undefined && modal.message !== "" &&
            h("div", { className: "qgd-modal-body" }, modal.message),
          isPrompt &&
            h("input", {
              ref: promptInputRef,
              className: "qgd-modal-input",
              type: "text",
              defaultValue: modal.initial || "",
              placeholder: modal.placeholder || "",
              autoFocus: true,
              onKeyDown: function (e) { if (e.key === "Enter") modal.onOk(); },
            }),
          h("div", { className: "qgd-modal-foot" },
            h("span", { className: "qgd-btn ghost", onClick: function () { setModal(null); } }, modal.cancelText || "取消"),
            h("span", { className: "qgd-btn ok", onClick: function () { modal.onOk(); } }, modal.okText || "确定")
          )
        )
      );
    }

    var chStatus = (data && data.channel_status) || {};

    return h("div", { id: uid, className: "qgd-root", style: { padding: 18 } },
      h("div", { className: "qgd-head" },
        h("h1", {}, "🛡️ AI 守护者",
          h("span", { className: "qgd-ver" }, "v" + PLUGIN_VERSION)),
        h("div", {},
          h("span", { className: "qgd-reload", onClick: load }, "刷新"),
          h("label", { style: { fontSize: 12, color: "#7d8aa0", marginLeft: 10 } },
            h("input", { type: "checkbox", checked: auto, onChange: function (e) { setAuto(e.target.checked); } }), " 自动刷新")
        )
      ),
      err && h("div", { style: { color: "#d64545", marginBottom: 8 } }, err),

      h("div", { className: "qgd-card" },
        h("h2", {}, "总览"),
        h("div", { className: "qgd-stats" }, statRow)
      ),

      h("div", { className: "qgd-card" },
        h("h2", {}, "Agent 与活动会话"),
        h("div", { className: "qgd-toolbar" },
          h("div", { className: "qgd-filter" }, filterButtons),
          h("span", { className: "qgd-btn ghost", onClick: function () { setShowArchived(!showArchived); } }, (showArchived ? "隐藏" : "显示") + "已归档 (" + Object.keys(archivedSet).length + ")")
        ),
        agentCards.length ? agentCards : h("div", { className: "qgd-empty" }, "（暂无 agent）")
      ),

      h("div", { className: "qgd-card" },
        h("h2", {}, "通知配置（飞书）"),
        h("div", { style: { marginBottom: 8 } },
          (chStatus.channel && chStatus.channel !== "feishu")
            ? h("div", { className: "qgd-status-no" }, "当前频道: " + chStatus.channel + "（非飞书，需切换为 feishu）")
            : (chStatus.configured
              ? h("div", { className: "qgd-status-ok" }, "✓ 飞书已配置" + (chStatus.app_id ? " · app_id=" + chStatus.app_id : "") + "，发送时将自动复用固定会话")
              : h("div", { className: "qgd-status-no" }, "✗ 未检测到飞书配置，请先在 agent.json 配置 feishu（app_id / app_secret）"))
        ),
        h("div", { className: "qgd-form" },
          h("div", {},
            h("label", {}, "频道"),
            h("input", { value: form.channel, onChange: function (e) { setForm(Object.assign({}, form, { channel: e.target.value })); } })
          ),
          h("div", {},
            h("label", {}, "话题群（oc_xxx）"),
            h("div", { style: { display: "flex", gap: 6, alignItems: "center", flexWrap: "wrap" } },
              topics.length > 1
                ? h("select", { value: form.feishu_topic_chat_id, onChange: function (e) { setForm(Object.assign({}, form, { feishu_topic_chat_id: e.target.value })); } },
                    h("option", { value: "" }, "— 请选择话题群 —"),
                    topics.map(function (t, i) {
                      return h("option", { value: t.chat_id, key: i }, (t.name || "") + " · " + t.chat_id);
                    }))
                : h("input", { style: { flex: 1, minWidth: 220 }, placeholder: "点「自动检测」自动填入", value: form.feishu_topic_chat_id, onChange: function (e) { setForm(Object.assign({}, form, { feishu_topic_chat_id: e.target.value })); } }),
              h("button", { onClick: discoverTopics, style: { background: "#3f7cbf" } },
                form.feishu_topic_chat_id ? "🔄 重新检测" : "🔍 自动检测"),
              h("button", { onClick: saveConfig }, "保存"),
              h("button", { onClick: testSend, style: { marginLeft: 6, background: "#1f9d61" } }, "测试发送")
            )
          ),
          h("div", { className: "qgd-msg", style: { flexBasis: "100%" } }, msg),
          h("div", { style: { flexBasis: "100%" } },
            h("label", {}, "发送模式"),
            h("select", { value: form.send_mode || "reply", onChange: function (e) { var v = e.target.value; setForm(Object.assign({}, form, { send_mode: v })); applyConfig({ send_mode: v }, true); } },
              h("option", { value: "reply" }, "回帖（复用专属主题帖，推荐）"),
              h("option", { value: "new_topic" }, "新话题（每次直发开新主题）")
            ),
          )
        ),
        renderModal()
      )
    );
  }

  function stat(key, ic, n, label) {
    return h("div", { className: "qgd-stat", key: key },
      h("div", { className: "qgd-stat-ic" }, ic),
      h("div", { className: "qgd-stat-tx" },
        h("b", {}, String(n)),
        h("span", {}, label)));
  }

  // ---------- 注册三件套 ----------
  if (QP.registerRoutes) {
    try {
      QP.registerRoutes(PLUGIN_ID, [{
        path: "/apps/" + PLUGIN_ID,
        component: GuardBoardComponent,
        label: PLUGIN_NAME,
        icon: "🛡️",
      }]);
      console.info("[qwenpaw-guard] registered via registerRoutes");
    } catch (e) { console.warn("[qwenpaw-guard] registerRoutes failed:", e); }
  }

  if (QP.route && QP.route.add) {
    try {
      QP.route.add(PLUGIN_ID, [{
        id: PLUGIN_ID + ".home",
        path: "/plugin/" + PLUGIN_ID,
        component: GuardBoardComponent,
      }]);
      console.info("[qwenpaw-guard] registered via route.add");
    } catch (e) { console.warn("[qwenpaw-guard] route.add failed:", e); }
  }

  if (QP.menu && QP.menu.add) {
    try {
      QP.menu.add(PLUGIN_ID, [{
        id: PLUGIN_ID + ".menu",
        location: "primary.settings",
        label: PLUGIN_NAME,
        icon: function () { return h("span", { className: "qwenpaw-menu-item-icon", style: { fontSize: 16 } }, "🛡️"); },
        route: PLUGIN_ID + ".home",
        order: 71,
      }]);
      console.info("[qwenpaw-guard] registered via menu.add");
    } catch (e) { console.warn("[qwenpaw-guard] menu.add failed:", e); }
  }

  console.info("[qwenpaw-guard] v" + PLUGIN_VERSION + " UI loaded");
})();

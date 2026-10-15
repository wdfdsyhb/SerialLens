/* SerialLens Web 智能体前端：自包含（无 CDN），先转义再格式化，防注入 */

(function () {
  "use strict";

  var chatEl = document.getElementById("chat");
  var inputEl = document.getElementById("input");
  var sendEl = document.getElementById("send");
  var statusDot = document.getElementById("statusDot");
  var busy = false;

  // ---------- mini markdown（标题/粗体/行内码/代码块/列表/换行） ----------
  function escapeHtml(s) {
    return String(s).replace(/[&<>"']/g, function (c) {
      return { "&": "&amp;", "<": "&lt;", ">": "&gt;", '"': "&quot;", "'": "&#39;" }[c];
    });
  }

  function renderMarkdown(src) {
    var lines = escapeHtml(src).split("\n");
    var out = [];
    var inCode = false;
    var codeBuf = [];
    var listMode = null; // "ul" | "ol"

    function closeList() {
      if (listMode) { out.push("</" + listMode + ">"); listMode = null; }
    }

    function inline(t) {
      return t
        .replace(/`([^`]+)`/g, "<code>$1</code>")
        .replace(/\*\*([^*]+)\*\*/g, "<strong>$1</strong>");
    }

    lines.forEach(function (raw) {
      var line = raw.replace(/\s+$/, "");
      if (line.trim().startsWith("```")) {
        if (inCode) {
          out.push("<pre><code>" + codeBuf.join("\n") + "</code></pre>");
          codeBuf = []; inCode = false;
        } else {
          closeList(); inCode = true;
        }
        return;
      }
      if (inCode) { codeBuf.push(line); return; }

      var h = line.match(/^(#{1,4})\s+(.*)$/);
      if (h) { closeList(); out.push("<h3>" + inline(h[2]) + "</h3>"); return; }

      var ol = line.match(/^\s*\d+\.\s+(.*)$/);
      if (ol) {
        if (listMode !== "ol") { closeList(); out.push("<ol>"); listMode = "ol"; }
        out.push("<li>" + inline(ol[1]) + "</li>"); return;
      }

      var ul = line.match(/^\s*[-*]\s+(.*)$/);
      if (ul) {
        if (listMode !== "ul") { closeList(); out.push("<ul>"); listMode = "ul"; }
        out.push("<li>" + inline(ul[1]) + "</li>"); return;
      }

      closeList();
      if (line.trim() === "") { return; }
      out.push("<p>" + inline(line) + "</p>");
    });
    if (inCode && codeBuf.length) { out.push("<pre><code>" + codeBuf.join("\n") + "</code></pre>"); }
    closeList();
    return out.join("");
  }

  // ---------- 消息渲染 ----------
  function addUserMsg(text) {
    var wrap = document.createElement("div");
    wrap.className = "msg user";
    var b = document.createElement("div");
    b.className = "bubble";
    b.textContent = text;
    wrap.appendChild(b);
    chatEl.appendChild(wrap);
    scrollBottom();
  }

  function addTyping() {
    var wrap = document.createElement("div");
    wrap.className = "msg bot";
    wrap.id = "typing-msg";
    wrap.innerHTML = '<div class="bubble"><span class="typing">分析中<span class="dots"></span></span></div>';
    chatEl.appendChild(wrap);
    scrollBottom();
    return wrap;
  }

  function codeBlock(code) {
    var pre = document.createElement("pre");
    var codeEl = document.createElement("code");
    codeEl.textContent = code;
    pre.appendChild(codeEl);
    var btn = document.createElement("button");
    btn.className = "copy-btn";
    btn.textContent = "复制";
    btn.onclick = function () {
      navigator.clipboard.writeText(code).then(function () {
        btn.textContent = "已复制";
        setTimeout(function () { btn.textContent = "复制"; }, 1500);
      });
    };
    pre.appendChild(btn);
    return pre;
  }

  function addFieldTable(fields) {
    var table = document.createElement("table");
    table.className = "field-table";
    var head = "<tr><th>字段</th><th>偏移</th><th>类型</th><th>比例</th></tr>";
    var rows = fields.map(function (f) {
      return "<tr><td>" + escapeHtml(f.name) + "</td><td>" + f.offset +
        "</td><td>" + escapeHtml(f.type) + "</td><td>×" + f.scale + "</td></tr>";
    }).join("");
    table.innerHTML = head + rows;
    return table;
  }

  function addBotMsg(result) {
    var wrap = document.createElement("div");
    wrap.className = "msg bot";
    var b = document.createElement("div");
    b.className = "bubble";

    var tag = document.createElement("span");
    tag.className = "tag";
    tag.textContent = result.title || result.scenario;
    b.appendChild(tag);

    var reply = document.createElement("div");
    reply.innerHTML = renderMarkdown(result.reply || "");
    b.appendChild(reply);

    if (result.steps && result.steps.length) {
      var det = document.createElement("details");
      det.className = "steps";
      det.innerHTML = "<summary>执行步骤（" + result.steps.length + " 步）</summary>";
      var ol = document.createElement("ol");
      result.steps.forEach(function (s) {
        var li = document.createElement("li");
        li.textContent = s;
        ol.appendChild(li);
      });
      det.appendChild(ol);
      b.appendChild(det);
    }

    var data = result.data || {};

    if (data.normal && data.garbled) {  // diagnose
      b.appendChild(h3("正常接收（115200）"));
      b.appendChild(codeBlock(data.normal.hexdump));
      b.appendChild(h3("乱码段（位错位）"));
      b.appendChild(codeBlock(data.garbled.hexdump));
    }

    if (data.hexdump && !data.c_header) {  // analyze
      b.appendChild(h3("采样 HEX 视图"));
      b.appendChild(codeBlock(data.hexdump));
    }

    if (data.fields) {  // codegen
      b.appendChild(h3("推断字段表"));
      b.appendChild(addFieldTable(data.fields));
    }
    if (data.c_header) {
      b.appendChild(h3("web_demo_sensor_parser.h"));
      b.appendChild(codeBlock(data.c_header));
      b.appendChild(h3("web_demo_sensor_parser.c"));
      b.appendChild(codeBlock(data.c_source));
    }

    if (result.summary && result.summary.trim() && !result.llm_polished) {
      var sdet = document.createElement("details");
      sdet.className = "steps";
      sdet.innerHTML = "<summary>技术摘要</summary>";
      var sd = document.createElement("div");
      sd.innerHTML = renderMarkdown(result.summary);
      sdet.appendChild(sd);
      b.appendChild(sdet);
    }

    wrap.appendChild(b);
    chatEl.appendChild(wrap);
    scrollBottom();
  }

  function h3(text) {
    var el = document.createElement("h3");
    el.textContent = text;
    return el;
  }

  function scrollBottom() {
    chatEl.scrollTop = chatEl.scrollHeight;
  }

  // ---------- 交互 ----------
  function send(text) {
    if (busy || !text.trim()) return;
    busy = true;
    sendEl.disabled = true;
    var welcome = document.getElementById("welcome");
    if (welcome) welcome.remove();

    addUserMsg(text.trim());
    var typing = addTyping();

    fetch("/api/chat", {
      method: "POST",
      headers: { "Content-Type": "application/json" },
      body: JSON.stringify({ message: text })
    })
      .then(function (r) { return r.json(); })
      .then(function (result) {
        typing.remove();
        addBotMsg(result);
      })
      .catch(function () {
        typing.remove();
        var wrap = document.createElement("div");
        wrap.className = "msg bot";
        wrap.innerHTML = '<div class="bubble"><p>服务暂时不可用，请稍后再试。</p></div>';
        chatEl.appendChild(wrap);
        scrollBottom();
      })
      .finally(function () {
        busy = false;
        sendEl.disabled = false;
        inputEl.focus();
      });
  }

  sendEl.onclick = function () { send(inputEl.value); inputEl.value = ""; };
  inputEl.addEventListener("keydown", function (e) {
    if (e.key === "Enter" && !e.shiftKey) {
      e.preventDefault();
      send(inputEl.value);
      inputEl.value = "";
    }
  });

  Array.prototype.forEach.call(document.querySelectorAll(".chip"), function (chip) {
    chip.onclick = function () { send(chip.getAttribute("data-q")); };
  });

  inputEl.addEventListener("input", function () {
    inputEl.style.height = "auto";
    inputEl.style.height = Math.min(inputEl.scrollHeight, 120) + "px";
  });

  // 健康检查点亮状态灯
  fetch("/api/health").then(function (r) {
    if (r.ok) statusDot.classList.add("online");
  }).catch(function () { /* 离线保持灰色 */ });

  // ---------- 主题切换（localStorage 记忆） ----------
  var themeBtn = document.getElementById("themeBtn");

  function applyTheme(theme) {
    document.body.dataset.theme = theme;
    try { localStorage.setItem("sl-theme", theme); } catch (e) { /* 隐私模式忽略 */ }
  }
  var savedTheme = null;
  try { savedTheme = localStorage.getItem("sl-theme"); } catch (e) { /* ignore */ }
  if (savedTheme === "dark" || savedTheme === "light") applyTheme(savedTheme);
  themeBtn.onclick = function () {
    applyTheme(document.body.dataset.theme === "dark" ? "light" : "dark");
  };

  // ---------- AI 设置弹窗 ----------
  var settingsBtn = document.getElementById("settingsBtn");
  var modal = document.getElementById("settingsModal");
  var cfgBase = document.getElementById("cfgBase");
  var cfgKey = document.getElementById("cfgKey");
  var cfgModel = document.getElementById("cfgModel");
  var cfgSave = document.getElementById("cfgSave");
  var cfgClose = document.getElementById("cfgClose");
  var llmBadge = document.getElementById("llmBadge");

  function refreshSettingsState() {
    fetch("/api/settings").then(function (r) { return r.json(); }).then(function (s) {
      llmBadge.textContent = s.configured ? "已配置" : "未配置";
      llmBadge.classList.toggle("on", !!s.configured);
      if (s.base_url) cfgBase.placeholder = s.base_url;
      if (s.model) cfgModel.placeholder = s.model;
    }).catch(function () { /* ignore */ });
  }
  refreshSettingsState();

  settingsBtn.onclick = function () {
    cfgKey.value = "";
    refreshSettingsState();
    modal.hidden = false;
    cfgBase.focus();
  };
  cfgClose.onclick = function () { modal.hidden = true; };
  modal.addEventListener("click", function (e) {
    if (e.target === modal) modal.hidden = true;
  });

  cfgSave.onclick = function () {
    cfgSave.disabled = true;
    cfgSave.textContent = "保存中…";
    fetch("/api/settings", {
      method: "POST",
      headers: { "Content-Type": "application/json" },
      body: JSON.stringify({
        base_url: cfgBase.value.trim(),
        api_key: cfgKey.value.trim(),
        model: cfgModel.value.trim(),
      })
    })
      .then(function (r) {
        if (!r.ok) throw new Error("save failed");
        return r.json();
      })
      .then(function (s) {
        llmBadge.textContent = s.configured ? "已配置" : "未配置";
        llmBadge.classList.toggle("on", !!s.configured);
        cfgKey.value = "";
        modal.hidden = true;
      })
      .catch(function () { alert("保存失败：请检查 API Key 是否填写"); })
      .finally(function () {
        cfgSave.disabled = false;
        cfgSave.textContent = "保存";
      });
  };

  inputEl.focus();
})();

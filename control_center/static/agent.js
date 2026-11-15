"use strict";
(() => {
  const el = id => document.getElementById(id), esc = escapeHTML;
  const roleLabels = {pm:"项目经理",video:"视频制作",site:"官网运维"};
  const stageStates = {ok:"已完成",running:"执行中",queued:"排队中",failed:"失败",quota_limited:"额度受限",cancelled:"已取消"};
  const stateTone = state => ({done:"green",draft_ready:"green",running:"blue",queued:"purple",failed:"red",review_rejected:"red",cancelled:"gray",partial:"orange",waiting_approval:"orange",waiting_publish_link:"orange",needs_info:"orange"}[state] || "gray");
  const badge = (text, color) => `<span class="status-pill" data-tone="${color}">${esc(text)}</span>`;
  let data = null, busy = false, episode = null, selectedTicket = new URLSearchParams(location.search).get("ticket");
  let accountsShape = "", listShape = "", detailShape = "";
  let resetDraft = null, resetSending = false, trackedReset = null;
  const accountName = id => data?.accounts.find(a => a.id === id)?.label || (id === "claude" ? "Claude（历史记录）" : id) || "待分配";
  const ticketHref = id => StudioNavigation.viewHref("tickets",{ticket:id});

  function accounts() {
    const shape = JSON.stringify(data.accounts);
    if (shape === accountsShape) return;
    accountsShape = shape;
    el("account-grid").innerHTML = data.accounts.map(a => {
      const stale = a.status === "stale", color = stale ? "orange" : a.status === "ok" ? "green" : a.status === "loading" ? "blue" : "red";
      const status = {ok:"已读取",stale:"上次读数 · 已过期",error:"读取失败",loading:"正在读取",disabled:"已停用"}[a.status];
      const windows = a.windows.map(w => {
        const remaining = w.remaining_percent, low = remaining != null && remaining <= 10;
        return `<div class="quota-window" data-tone="${stale ? "orange" : low ? "red" : "blue"}"><div><span>${esc(w.label)}${w.bucket !== "codex" ? " · "+esc(w.bucket) : ""}</span><strong>${remaining == null ? "未知" : esc(remaining)+"%"}<small> 剩余</small></strong></div>${remaining == null ? "" : `<progress max="100" value="${remaining}" aria-label="${esc(a.label)} ${esc(w.label)}剩余 ${remaining}%"></progress>`}<p>${w.resets_at ? "重置 "+formatTime(w.resets_at) : "重置时间未知"}</p></div>`;
      }).join("");
      const credit = a.reset_credits, count = credit?.available_count, operation = a.reset_operation;
      const pending = operation?.status === "unknown", running = operation?.status === "running", unavailable = operation?.status === "unavailable";
      const canReset = !data.lan_read_only && !resetSending && (pending || (a.status === "ok" && count > 0 && a.email)) && !running && !unavailable;
      const expires = credit?.credits?.[0]?.expires_at;
      const resetArea = `<div class="account-reset"><div><span>可用重置机会</span><strong>${count == null ? "未知" : esc(count)+" 次"}</strong></div>${expires ? `<p>最近到期 ${formatTime(expires)}</p>` : ""}<button class="reset-button" data-reset-account="${esc(a.id)}" type="button" ${canReset ? "" : "disabled"}>${running ? "正在重置…" : pending ? "重试确认重置结果" : "手动重置额度"}</button>${operation?.message ? `<p class="reset-operation" data-state="${esc(operation.status)}">${esc(operation.message)}</p>` : count === 0 ? '<p>当前没有可用重置机会</p>' : count == null ? '<p>等待官方返回可用次数</p>' : ""}</div>`;
      return `<article class="account-card"><div class="team-card-top"><h3>${esc(a.label)}</h3>${badge(status,color)}</div><p class="account-email">${esc(a.email || (a.status === "loading" ? "正在核对账号身份…" : "账号身份暂不可读"))}</p><p class="account-role">${a.primary_roles.map(r=>esc(roleLabels[r] || r)).join(" / ") || "备用账号"}${a.plan ? " · "+esc(a.plan.toUpperCase()) : ""}</p>${windows || '<p class="quota-empty">'+(a.status === "loading" ? "正在获取官方额度…" : "暂无可用额度读数")+'</p>'}${resetArea}${a.message ? `<p class="account-warning">${esc(a.message)}</p>` : ""}<p class="account-time">${a.checked_at ? "最近成功 "+formatTime(a.checked_at) : "尚未成功读取"}${a.refreshing && a.checked_at ? " · 更新中" : ""}</p></article>`;
    }).join("") || '<p class="muted">未配置 Codex 账号。</p>';
  }

  function openReset(id) {
    const account = data?.accounts.find(a=>a.id === id);
    if (!account || resetSending || data.lan_read_only) return;
    const previous = account.reset_operation;
    const retry = previous?.status === "unknown";
    if (!retry && (account.status !== "ok" || !(account.reset_credits?.available_count > 0))) return;
    const credit = account.reset_credits?.credits?.[0];
    resetDraft = {account:id, email:retry ? previous.email : account.email,
                  credit_id:retry ? previous.credit_id : credit?.id || null,
                  idempotency_key:retry ? previous.idempotency_key : crypto.randomUUID(), confirmed:true};
    el("reset-dialog-account").textContent = `${account.label} · ${resetDraft.email}`;
    el("reset-dialog-copy").textContent = retry ? "确认重试上次操作。会沿用同一操作记录，查询并完成原来的重置。" : `本次将使用此账号 1 次已有的重置机会，重置符合条件的额度窗口。当前可用 ${account.reset_credits.available_count} 次。`;
    el("reset-dialog-credit").textContent = retry ? "上次操作结果待确认，重试会避免重复使用机会。" : credit ? `${credit.title}${credit.expires_at ? " · 到期 "+formatTime(credit.expires_at) : ""}${credit.description ? "\n"+credit.description : ""}` : "官方将选择此账号下一次可用的重置机会。";
    el("reset-confirm").textContent = retry ? "重试确认结果" : "使用 1 次重置机会";
    el("reset-dialog").showModal();
  }

  function resetFeedback(message) {
    el("reset-feedback").textContent = message;
    el("reset-feedback").hidden = false;
  }

  async function submitReset() {
    if (!resetDraft || resetSending) return;
    const request = resetDraft;
    resetSending = true;
    el("reset-confirm").disabled = true;
    try {
      const {account,...payload} = request;
      const response = await fetch(`/api/accounts/${encodeURIComponent(account)}/reset`,{method:"POST",headers:{"Content-Type":"application/json","X-Studio-Request":"quota-reset","X-Studio-CSRF":data.reset_token},body:JSON.stringify(payload)});
      const result = await response.json();
      if (!response.ok) throw new Error(result.error || "无法提交重置操作");
      trackedReset = {account, key:request.idempotency_key};
      resetFeedback(`${accountName(account)}：${result.message || "正在执行重置，请等待结果…"}`);
      el("reset-dialog").close();
      resetDraft = null;
    } catch(error) {
      resetFeedback(`${error.message}。请刷新后检查此账号的操作状态。`);
      el("reset-dialog").close();
      resetDraft = null;
    } finally {
      resetSending = false;
      el("reset-confirm").disabled = false;
      accountsShape = "";
      await refreshAgent();
    }
  }

  function episodeLinks(links) {
    return links.map(ep=>`<a class="ticket-episode-link" data-episode="${esc(ep.ref)}" href="${esc(StudioNavigation.viewHref("studio",{episode:ep.ref,panel:"overview",...(selectedTicket ? {ticket:selectedTicket} : {})}))}" title="${esc(ep.title)}">↗ Episode ${esc(ep.id)} <small>${esc(ep.source)}</small></a>`).join("") || '<span class="muted">尚无明确 Episode 关联</span>';
  }

  function related() {
    const tickets = (data?.tickets || []).filter(t=>t.episodes.some(e=>e.ref === episode));
    el("episode-tickets").innerHTML = tickets.length ? `<span>关联电报工单</span> ${tickets.map(t=>`<a href="${ticketHref(t.id)}" data-ticket="${esc(t.id)}">${esc(t.id)} · ${esc(t.state_label)}</a>`).join("")}` : '<span>暂无明确关联的电报工单</span>';
  }

  function list() {
    if (!data) return;
    const query = el("ticket-search").value.trim().toLowerCase(), state = el("ticket-state").value;
    const visible = data.tickets.filter(t => {
      if (el("ticket-episode-only").checked && !t.episodes.some(e=>e.ref === episode)) return false;
      if (query && !`${t.id} ${t.title} ${t.episodes.map(e=>`${e.id} ${e.title}`).join(" ")}`.toLowerCase().includes(query)) return false;
      if (state === "active" && ["done","draft_ready","cancelled","failed","review_rejected"].includes(t.state)) return false;
      if (state === "done" && !["done","draft_ready"].includes(t.state)) return false;
      if (state === "failed" && !["failed","review_rejected"].includes(t.state)) return false;
      if (!["all","active","done","failed"].includes(state) && t.state !== state) return false;
      return true;
    });
    el("ticket-list-note").textContent = `显示 ${visible.length} / ${data.tickets.length} 条${data.total > data.tickets.length ? ` · 共 ${data.total} 条，仅载入最近 200 条及指定工单` : ""}`;
    const shape = JSON.stringify([visible,selectedTicket]);
    if (shape === listShape) return;
    listShape = shape;
    el("ticket-list").innerHTML = visible.map(t=>`<a class="ticket-row ${t.id === selectedTicket ? "selected" : ""}" href="${ticketHref(t.id)}" data-ticket="${esc(t.id)}" ${t.id === selectedTicket ? 'aria-current="true"' : ""}><div class="ticket-row-top"><strong>${esc(t.id)}</strong>${badge(t.state_label,stateTone(t.state))}</div><p>${esc(t.title || "无内容")}</p><div class="ticket-row-foot"><span>${esc(t.stage_label)} · ${esc(accountName(t.executor))}</span><time>${formatTime(t.updated_at)}</time></div>${t.episodes.length ? `<span class="ticket-ep-chip">Episode ${t.episodes.map(e=>esc(e.id)).join(" / ")}</span>` : ""}</a>`).join("") || '<p class="empty-tickets">没有符合筛选条件的工单。</p>';
  }

  function detail() {
    const t = data.ticket, shape = JSON.stringify([t,selectedTicket,data.accounts.map(a=>[a.id,a.label])]);
    if (shape === detailShape) return;
    detailShape = shape;
    if (!t) { el("ticket-detail").innerHTML = '<p class="muted">工单不存在或尚无工单。</p>'; return; }
    el("ticket-detail").innerHTML = `<div class="ticket-detail-top"><div><p class="eyebrow">WORK ORDER</p><h3 id="ticket-detail-title" tabindex="-1">${esc(t.id)} · ${esc(t.role_label)}</h3></div>${badge(t.state_label,stateTone(t.state))}</div><div class="ticket-links">${episodeLinks(t.episodes)}</div><p class="ticket-request">${esc(t.request)}</p><div class="ticket-summary"><span>当前阶段 <strong>${esc(t.stage_label)}</strong></span><span>完成阶段 <strong>${t.completed_stages} 次</strong></span><span>执行尝试 <strong>${t.attempts} 次</strong></span><span>当前轮次 <strong>${t.round}</strong></span></div>${t.simulated ? '<p class="account-warning">此工单包含模拟执行记录。</p>' : ""}<p class="small-note">展示已记录的阶段与日志；单个阶段没有可靠的百分比或预计完成时间。</p><ol class="stage-timeline">${t.stages.map(s=>`<li data-state="${esc(s.status)}"><div class="stage-heading"><strong>${esc(s.label)}</strong>${badge(stageStates[s.status] || s.status,s.status === "ok" ? "green" : s.status === "running" ? "blue" : s.status === "failed" ? "red" : "orange")}</div><p class="stage-meta">${esc(accountName(s.executor))} · 第 ${s.round} 轮 / 第 ${s.attempt} 次${s.simulated ? " · 模拟" : ""}</p><p class="stage-meta">${s.started_at ? "开始 "+formatTime(s.started_at) : "待开始"}${s.finished_at ? " · 结束 "+formatTime(s.finished_at) : s.last_heartbeat ? " · 最近心跳 "+formatTime(s.last_heartbeat) : ""}</p>${s.summary ? `<p class="stage-summary">${esc(s.summary)}</p>` : ""}${s.error ? `<p class="account-warning">${esc(s.error)}</p>` : ""}${s.progress?.length ? `<details ${s.status === "running" ? "open" : ""}><summary>最近进度 · 最多 12 行</summary><pre class="progress-log">${esc(s.progress.join("\n"))}</pre></details>` : ""}${s.artifacts.length ? `<details><summary>交付记录 · ${s.artifacts.length} 项</summary><ul class="ticket-artifacts">${s.artifacts.map(a=>`<li>${esc(a.name)} <small>${size(a.bytes)}</small></li>`).join("")}</ul></details>` : ""}</li>`).join("") || '<li class="muted">尚未开始执行，等待调度。</li>'}</ol>`;
  }

  function render() {
    document.dispatchEvent(new CustomEvent("agent-snapshot",{detail:data}));
    const reachable = data.scheduler_reachable;
    el("scheduler-state").textContent = data.status === "ok" ? reachable ? "调度器端口监听中" : reachable === false ? "调度器端口未监听" : "调度器状态未知" : "数据源未连接";
    el("scheduler-state").dataset.tone = data.status === "ok" && reachable ? "green" : "orange";
    el("agent-note").textContent = `${data.message}${data.video_mode === "synthetic" ? " · 当前视频模式：合成素材" : ""}${data.status === "ok" && !reachable ? " · 以下为已保存记录，执行状态可能已过期" : ""}`;
    const n = data.counts, sums = [["全部",data.total],["执行中",n.running || 0],["排队",n.queued || 0],["待人工",(n.partial || 0)+(n.waiting_approval || 0)+(n.waiting_publish_link || 0)+(n.needs_info || 0)],["完成 / 草稿",(n.done || 0)+(n.draft_ready || 0)],["异常",(n.failed || 0)+(n.review_rejected || 0)]];
    el("ticket-counts").innerHTML = sums.map(([label,num])=>`<div><strong>${num}</strong><span>${label}</span></div>`).join("");
    el("agent-scan-time").textContent = `最近读取 ${formatTime(data.scanned_at)} · 工单每 4 秒刷新 · 额度每 5 分钟读取 · 仅本机展示`;
    accounts(); list(); detail(); related();
    if (trackedReset) {
      const a = data.accounts.find(a=>a.id === trackedReset.account), op = a?.reset_operation;
      if (op && (op.idempotency_key === trackedReset.key || op.status === "unavailable")) resetFeedback(`${a.label}：${op.message}`);
    }
  }

  async function refreshAgent() {
    if (busy) return;
    busy = true;
    const requested = selectedTicket;
    try {
      const response = await fetch(`/api/agent${requested ? "?ticket="+encodeURIComponent(requested) : ""}`,{cache:"no-store"});
      if (!response.ok) throw new Error("无法读取工单接口");
      const next = await response.json();
      if (requested !== selectedTicket) return;
      data = next;
      if (!selectedTicket && data.ticket) selectedTicket = data.ticket.id;
      render();
    } catch (_) {
      el("agent-note").textContent = "工单连接中断；保留上次记录，显示的执行状态和额度可能已过期。";
      el("scheduler-state").textContent = "读取失败";
      el("scheduler-state").dataset.tone = "orange";
      if (data) { data.accounts = data.accounts.map(a=>({...a,status:a.checked_at ? "stale" : "error"})); accounts(); document.dispatchEvent(new CustomEvent("agent-snapshot",{detail:{...data,status:"unavailable",message:"电报连接中断，保留上次记录。"}})); }
    } finally {
      busy = false;
      if (requested !== selectedTicket && data?.ticket?.id !== selectedTicket) refreshAgent();
      else if (data?.accounts.some(a=>a.reset_operation?.status === "running")) setTimeout(refreshAgent,2000);
    }
  }

  document.addEventListener("studio-snapshot", event => { episode = event.detail.episode?.ref || null; list(); related(); });
  document.addEventListener("click", event => {
    const resetButton = event.target.closest("[data-reset-account]");
    if (resetButton && !resetButton.disabled) { openReset(resetButton.dataset.resetAccount); return; }
    const link = event.target.closest("[data-ticket]");
    if (!link || event.ctrlKey || event.metaKey || event.shiftKey || event.button !== 0) return;
    event.preventDefault(); selectedTicket = link.dataset.ticket; StudioNavigation.navigate(link.href); list();
    refreshAgent().then(()=> { el("ticket-detail-title")?.focus({preventScroll:true}); if (innerWidth < 900) el("ticket-detail").scrollIntoView({behavior:"smooth",block:"start"}); });
  });
  window.addEventListener("popstate",()=> { selectedTicket = new URLSearchParams(location.search).get("ticket"); refreshAgent(); });
  el("ticket-search").addEventListener("input",list);
  el("ticket-state").addEventListener("change",list);
  el("ticket-episode-only").addEventListener("change",list);
  el("refresh").addEventListener("click",refreshAgent);
  el("reset-cancel").addEventListener("click",()=> { el("reset-dialog").close(); resetDraft = null; });
  el("reset-dialog").addEventListener("cancel",()=> { resetDraft = null; });
  el("reset-confirm").addEventListener("click",submitReset);
  setInterval(()=>{ if (el("auto-refresh").checked && !document.hidden) refreshAgent(); },4000);
  document.addEventListener("visibilitychange",()=> { if (!document.hidden && el("auto-refresh").checked) refreshAgent(); });
  refreshAgent();
})();

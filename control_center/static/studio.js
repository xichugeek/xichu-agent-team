"use strict";
const $ = id => document.getElementById(id);
let selected = new URLSearchParams(location.search).get("episode");
let current = null, fetching = false, lastShape = "", episodeShape = "", lanReadOnly = false;
const escapeHTML = value => String(value ?? "UNKNOWN").replace(/[&<>"']/g, ch => ({"&":"&amp;","<":"&lt;",">":"&gt;",'"':"&quot;","'":"&#39;"}[ch]));
const formatTime = value => { if (!value) return "UNKNOWN"; const d = new Date(value); return Number.isNaN(d.getTime()) ? "UNKNOWN" : new Intl.DateTimeFormat("zh-CN",{month:"2-digit",day:"2-digit",hour:"2-digit",minute:"2-digit",second:"2-digit",hour12:false}).format(d); };
const tone = status => {
  const s = String(status).toLowerCase();
  if (["approved","published","selected","rendered","pass","found","ok","source_found","edit_ready_for_user_export","rendered_candidate"].includes(s)) return "green";
  if (["obsolete","blocked","rejected","fail","error","unsafe","validate_error","missing"].includes(s)) return "red";
  if (["review_required","review_required","stale","not_rendered","edit_record_requires_verification"].includes(s)) return "orange";
  if (["pending","waiting_for_human_review"].includes(s)) return "purple";
  if (["running","ready","ready_to_continue","busy"].includes(s)) return "blue";
  return "gray";
};
const pill = (status, label = status) => `<span class="status-pill" data-tone="${tone(status)}">${escapeHTML(label)}</span>`;
const size = bytes => bytes == null ? "—" : bytes < 1024 ? `${bytes} B` : bytes < 1024**2 ? `${(bytes/1024).toFixed(1)} KB` : bytes < 1024**3 ? `${(bytes/1024**2).toFixed(1)} MB` : `${(bytes/1024**3).toFixed(2)} GB`;
const duration = value => value == null ? "UNKNOWN" : `${Math.floor(value/60)}:${String(Math.floor(value%60)).padStart(2,"0")}`;

function episodeLink(ep) {
  return `<a class="episode-link ${ep.ref === current?.ref ? "active" : ""}" href="${escapeHTML(StudioNavigation.viewHref("studio",{episode:ep.ref}))}" data-episode="${escapeHTML(ep.ref)}" ${ep.ref === current?.ref ? 'aria-current="page"' : ""}><span class="ep-top"><span>${ep.test_only ? "TEST" : "EPISODE"} ${escapeHTML(ep.id)}</span><span class="ep-state">${ep.status === "PUBLISHED" ? "已发布" : ep.status === "UNKNOWN" ? "UNKNOWN" : "制作记录"}</span></span><span class="ep-title">${escapeHTML(ep.title)}</span></a>`;
}
function renderEpisodes(episodes) {
  const shape = JSON.stringify([episodes,current?.ref]);
  if (shape === episodeShape) return;
  episodeShape = shape;
  const formal = episodes.filter(ep => !ep.test_only), tests = episodes.filter(ep => ep.test_only);
  $("episode-count").textContent = formal.length;
  $("test-count").textContent = tests.length ? `(${tests.length})` : "";
  $("episode-list").innerHTML = formal.map(episodeLink).join("") || '<p class="muted">尚无正式 Episode</p>';
  $("test-list").innerHTML = tests.map(episodeLink).join("");
  $("episode-picker").innerHTML = [["正式期号",formal],["合成测试",tests]].filter(([,rows])=>rows.length).map(([label,rows])=>`<optgroup label="${label}">${rows.map(ep=>`<option value="${escapeHTML(ep.ref)}" ${ep.ref === current?.ref ? "selected" : ""}>${escapeHTML(ep.id)} · ${escapeHTML(ep.title)}</option>`).join("")}</optgroup>`).join("");
}
function renderPipeline(ep) {
  const icons = {director:"D",research:"R",script:"S",recorder:"REC",visual:"V",voice:"♪",editor:"E",qa:"QA",published:"✓"};
  const node = step => `<article class="flow-node ${step.kind === "human" ? "human" : ""} ${step.current ? "current" : ""}" data-node="${escapeHTML(step.id)}" data-tone="${tone(step.status)}" ${step.current ? 'aria-current="step"' : ""}>${step.current ? '<span class="current-indicator">CURRENT STEP</span>' : ""}<span class="node-icon" aria-hidden="true">${step.kind === "human" ? "人" : icons[step.id] || "·"}</span><div class="node-copy">${step.kind === "human" ? '<span class="human-marker">HUMAN REVIEW</span>' : ""}<strong>${escapeHTML(step.label)}</strong><small>${escapeHTML(step.note || (step.outputs.length ? step.outputs.join(" / ") : "由用户明确确认"))}</small></div><span class="node-badge">${escapeHTML(step.status_label)}</span></article>`;
  const before = ep.pipeline.slice(0,5), branches = ep.pipeline.slice(5,8), after = ep.pipeline.slice(8);
  $("pipeline").innerHTML = `<div class="flow-row sequence-row">${before.map(node).join("")}</div><div class="flow-row branch-row">${branches.map(node).join("")}</div><div class="flow-row sequence-row">${after.map(node).join("")}</div>`;
  $("progress-breakdown").innerHTML = ep.progress_breakdown.map(s => `<div class="breakdown-row"><span>${escapeHTML(s.label)}</span><span>${s.weight}%</span><span>${s.points}/${s.weight}</span></div>`).join("");
}
function renderTeam(ep) {
  $("team").innerHTML = ep.team.map(role => `<article class="team-card ${role.current ? "current" : ""}" data-role="${escapeHTML(role.id)}"><div class="team-card-top"><h3>${escapeHTML(role.name)}</h3>${pill(role.status,role.status_label)}</div><p class="team-role">${escapeHTML(role.label)}</p><p class="team-id">${escapeHTML(role.id)}</p><p class="card-label">OUTPUT</p><div class="team-output">${role.outputs.map(escapeHTML).join("<br>")}</div><p class="team-execution">本期：${escapeHTML(role.execution)}</p><p class="team-time">${role.last_delivery ? "最近交付记录："+formatTime(role.last_delivery) : "最近状态更新："+formatTime(role.last_state_update)}</p>${role.needs_redo ? '<p class="redo-note">需处理失效 / 重新制作</p>' : role.needs_review ? '<p class="review-note">有产物需要复核</p>' : '<p class="team-time">无需按当前状态重做</p>'}</article>`).join("");
}
function renderGates(ep) {
  $("gates").innerHTML = ["FACT_REVIEW","SCRIPT_APPROVAL","FINAL_REVIEW","PUBLISH_APPROVAL"].map(name => {
    const gate = ep.gates[name];
    return `<article class="gate-card" data-gate="${name}"><span class="human-marker">人工节点${gate.formal ? "" : " · 产物审批推导"}</span><h3>${name}</h3>${pill(gate.status,gate.status.toUpperCase())}<p>${name === "FACT_REVIEW" ? "facts + evidence 已批准才显示 APPROVED；非新增 Gate。" : gate.status === "approved" ? "读取已登记的人工签认；面板未执行批准。" : "等待本次明确人工签认；不能自动通过。"}</p></article>`;
  }).join("");
}
function renderValidate(ep) {
  const v = ep.validate;
  $("validate-status").textContent = v.status;
  $("validate-status").dataset.tone = tone(v.status);
  $("validate-note").textContent = v.checked_at ? `最近检查 ${formatTime(v.checked_at)}${v.status === "STALE" ? " · 文件已变化，请重检" : " · 仅表示文件契约校验结果"}` : v.status === "RUNNING" ? "正在只读校验；完成后自动刷新显示结果。" : "尚未运行。可手动检查当前文件、依赖与 Gate 一致性。";
  $("validate-issues").innerHTML = (v.issues || []).map(issue => `<li>${escapeHTML(issue)}</li>`).join("");
  $("run-validate").disabled = lanReadOnly || v.status === "RUNNING";
  $("run-validate").textContent = lanReadOnly ? "请在电脑本机运行 Validate" : v.status === "RUNNING" ? "Validate 正在运行…" : "运行只读 Validate";
}
function renderHealth(health) {
  $("health").innerHTML = health.map(item => `<div class="health-row"><span class="health-name">${escapeHTML(item.name)}${item.detail ? `<small>${escapeHTML(item.detail)}</small>` : ""}</span><span class="health-value ${["OK","FOUND","SOURCE_FOUND"].includes(item.status) ? "ok" : ["MISSING","NOT_FOUND"].includes(item.status) ? "missing" : ""}">${escapeHTML(item.status)}</span></div>`).join("");
}
function renderArtifacts(ep) {
  $("artifacts").innerHTML = ep.deliveries.map(item => `<tr data-artifact="${escapeHTML(item.id)}"><td><div class="file-label">${escapeHTML(item.label)}</div><div class="file-path">${escapeHTML(item.path ?? "UNKNOWN · 无选定输出")}</div></td><td class="${item.exists === true ? "exists-yes" : item.exists === false ? "exists-no" : "exists-unknown"}">${item.exists === true ? "存在" : item.exists === false ? "不存在" : "UNKNOWN"}</td><td>${pill(item.status)}</td><td>${formatTime(item.modified_at)}</td><td>${size(item.size)}</td></tr>`).join("");
}
function render(snapshot) {
  lanReadOnly = Boolean(snapshot.studio.lan_read_only);
  $("lan-mode").hidden = !lanReadOnly;
  current = snapshot.episode;
  document.dispatchEvent(new CustomEvent("studio-snapshot", {detail: snapshot}));
  renderEpisodes(snapshot.episodes);
  $("loading").hidden = true;
  if (!current) {
    $("content").hidden = true;
    $("loading").hidden = false;
    $("loading").textContent = "Episodes 目录暂无可识别记录；创建本期后刷新即可。";
    return;
  }
  selected = current.ref;
  $("content").hidden = false;
  $("episode-kicker").textContent = `${current.test_only ? "SYNTHETIC TEST" : current.ref === snapshot.studio.active_episode ? "CURRENT EPISODE" : "EPISODE RECORD"} / ${current.id}`;
  $("episode-title").textContent = `${current.id} · ${current.title}`;
  $("overall-status").textContent = current.overall_status.replaceAll("_"," ");
  $("overall-status").dataset.tone = tone(current.overall_status);
  $("current-stage").textContent = current.stage === "qa" ? "QA · 待复核" : current.stage;
  $("current-role").textContent = current.current_role;
  $("progress-value").textContent = `${current.progress}%`;
  $("progress").value = current.progress;
  $("next-action").textContent = current.next_action;
  $("last-updated").textContent = `状态更新时间 ${formatTime(current.status_modified_at)}`;
  $("validate-overview").textContent = `Studio Validate ${current.validate.status}`;
  $("validate-overview").className = tone(current.validate.status);
  $("blocker-count").textContent = `阻塞 ${current.blockers.length}`;
  $("obsolete-count").textContent = `失效 ${current.obsolete.length} · 待复核 ${current.review_required.length}`;
  const stats = current.stats;
  const chips = [
    ["镜头", stats.shots ?? "UNKNOWN", stats.shots != null],
    ["画面", stats.width && stats.height ? `${stats.width}×${stats.height} / ${stats.fps ?? "?"} FPS` : "UNKNOWN", !!stats.width],
    ["时长",duration(stats.duration), stats.duration != null],
    ["本人主音轨",current.candidate.master_exists ? "FOUND" : "未确认",current.candidate.master_exists],
    ["字幕",stats.captions == null ? current.candidate.captions_exist ? "FOUND" : "未确认" : `${stats.captions} 条`,current.candidate.captions_exist],
    ["封面",current.candidate.cover_exists ? stats.cover_version : "未确认",current.candidate.cover_exists],
    ["剪映工程",current.candidate.native_ready ? "已保存重开" : current.candidate.native_directory_exists ? "目录存在 / 待验证" : "未确认",current.candidate.native_ready],
    ["候选 MP4",current.candidate.video_ready ? "报告引用已核验" : current.candidate.render_status === "NOT_RENDERED" ? "NOT RENDERED" : "未确认",current.candidate.video_ready]
  ];
  $("delivery-strip").innerHTML = chips.map(([label,value,ok]) => `<span class="delivery-chip"><span class="${ok ? "check" : "warn"}">${ok ? "✓" : "·"}</span>${escapeHTML(label)}<strong>${escapeHTML(value)}</strong></span>`).join("");
  // Keep focus and open details stable during polling when no actual file state changed.
  const shape = JSON.stringify([current.ref,current.fingerprint,current.validate,current.overall_status,snapshot.health]);
  if (shape !== lastShape) {
    renderPipeline(current); renderTeam(current); renderGates(current); renderValidate(current); renderHealth(snapshot.health); renderArtifacts(current);
    $("warning-count").textContent = `(${current.warnings.length})`;
    $("diagnostics").innerHTML = current.warnings.map(w => `<p>· ${escapeHTML(w)}</p>`).join("") || '<p class="muted">当前轻量读取未发现缺失契约警告；完整一致性请运行 Validate。</p>';
    $("episode-directory").textContent = current.directory;
    lastShape = shape;
  }
  $("scan-time").textContent = `最近读取 ${formatTime(snapshot.studio.scanned_at)} · ${lanReadOnly ? "局域网只读" : "本机只读"}`;
}
async function refresh() {
  if (fetching) return;
  fetching = true;
  $("refresh").disabled = true;
  try {
    const response = await fetch(`/api/snapshot${selected ? "?episode="+encodeURIComponent(selected) : ""}`,{cache:"no-store"});
    const data = await response.json();
    if (!response.ok) throw new Error(data.error || `HTTP ${response.status}`);
    $("connection-error").hidden = true;
    render(data);
  } catch(error) {
    $("connection-error").hidden = false;
    $("connection-error").textContent = `读取失败：${error.message}。现有生产文件未修改；请检查本地服务后刷新。`;
    $("loading").hidden = true;
  } finally { fetching = false; $("refresh").disabled = false; }
}
document.addEventListener("click",event => {
  const link = event.target.closest("[data-episode]");
  if (!link || event.ctrlKey || event.metaKey || event.shiftKey || event.button !== 0) return;
  event.preventDefault();
  selected = link.dataset.episode;
  document.querySelector(".episode-browser").open = false;
  StudioNavigation.navigate(link.href);
  lastShape = "";
  refresh().then(() => { if (link.hash) document.getElementById(link.hash.slice(1))?.scrollIntoView({behavior:"smooth",block:"start"}); });
});
window.addEventListener("popstate",() => { selected = new URLSearchParams(location.search).get("episode"); lastShape=""; refresh(); });
$("episode-picker").addEventListener("change",()=> {
  selected = $("episode-picker").value;
  document.querySelector(".episode-browser").open = false;
  StudioNavigation.navigate(StudioNavigation.viewHref("studio",{episode:selected}));
  lastShape = ""; refresh();
});
$("refresh").addEventListener("click",refresh);
$("run-validate").addEventListener("click",async () => {
  if (!current || lanReadOnly) return;
  $("run-validate").disabled = true;
  try {
    const response = await fetch(`/api/episodes/${encodeURIComponent(current.ref)}/validate`,{method:"POST",headers:{"X-Studio-Request":"validate"},body:"{}"});
    const result = await response.json();
    if (![202,409].includes(response.status)) throw new Error(result.error || "Validate 启动失败");
    if (response.status === 409) { $("connection-error").textContent=result.message; $("connection-error").hidden=false; }
    await refresh();
  } catch(error) { $("connection-error").textContent=error.message; $("connection-error").hidden=false; $("run-validate").disabled=false; }
});
setInterval(() => { if ($("auto-refresh").checked && !document.hidden) refresh(); },4000);
document.addEventListener("visibilitychange",() => { if (!document.hidden && $("auto-refresh").checked) refresh(); });
refresh();

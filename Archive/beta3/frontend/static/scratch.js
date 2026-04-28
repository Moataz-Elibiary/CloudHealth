
>     <script>
          // --- WebSockets ---
          const ws = new WebSocket(`ws://${window.location.host}/ws/ui`);
          
          // --- DOM Elements ---
          const cardsContainer = document.getElementById('cards');
          const runBtn = document.getElementById('runBtn');
          const errorBanner = document.getElementById('errorBanner');
          const reportBanner = document.getElementById('reportBanner');
          
          // Config Elements
          const saveConfigBtn = document.getElementById('saveConfigBtn');
          const configMsg = document.getElementById('configMsg');
          
          const cfgMap = {
              parallel_limit: document.getElementById('cfg_parallel_limit'),
              max_parallel_nodes: document.getElementById('cfg_max_parallel_nodes'),
              ssh_timeout: document.getElementById('cfg_ssh_timeout'),
              cmd_timeout: document.getElementById('cfg_cmd_timeout'),
              input_dir: document.getElementById('cfg_input_dir'),
              output_dir: document.getElementById('cfg_output_dir'),
              inventory_file: document.getElementById('cfg_inventory_file'),
          };
          const thrMap = {
              disk_percent: document.getElementById('cfg_disk_percent'),
              mem_warn: document.getElementById('cfg_mem_warn'),
              mem_fail: document.getElementById('cfg_mem_fail'),
              swap_warn: document.getElementById('cfg_swap_warn'),
              load_warn: document.getElementById('cfg_load_warn'),
              load_fail: document.getElementById('cfg_load_fail')
          };
          
          // Filter Elements
          const filterBtns = document.querySelectorAll('.filter-btn');
          let currentFilter = 'all'; // all, error, warn, pass
          
          // State
          let stats = { pass: 0, warn: 0, fail: 0 };
          let clusters = {};
          let isRunning = false;
          let configData = {};
          let checkCategories = {};
          let saveTimeout = null;
  
          // --- Initialization ---
          async function init() {
              await loadConfig();
              await loadChecks();
          }
  
          async function loadConfig() {
              try {
                  const r = await fetch('/api/config');
                  const data = await r.json();
                  if(data.error) { console.error("Config load error:", data.error); return; }
                  configData = data;
                  
                  Object.keys(cfgMap).forEach(k => {
                      if (data[k] !== undefined && cfgMap[k]) {
                          cfgMap[k].value = data[k];
                      }
                  });
                  
                  if(data.thresholds) {
                      Object.keys(thrMap).forEach(k => {
                          if (data.thresholds[k] !== undefined && thrMap[k]) {
                              thrMap[k].value = data.thresholds[k];
                          }
                      });
                  }
              } catch (e) { console.error("Config fetch failed:", e); }
          }
  
          async function loadChecks() {
              try {
                  const r = await fetch('/api/checks');
                  checkCategories = await r.json();
                  renderCheckGroups();
                  
                  if (configData.last_enabled_checks && configData.last_enabled_checks.length > 0) {
                      document.querySelectorAll('.check-opt').forEach(c => c.checked = false);
                      configData.last_enabled_checks.forEach(id => {
                          const el = document.getElementById('chk_' + id);
                          if (el) el.checked = true;
                      });
                      Object.keys(checkCategories).forEach(groupName => {
                          const gid = groupName.replace(/[^a-z]/gi, '_');
                          updateCounter(gid);
                      });
                  }
              } catch (e) { console.error("Checks fetch failed:", e); }
          }
  
          // --- Sidebar Tabs Logic ---
          document.querySelectorAll('.sidebar-tab').forEach(tab => {
              tab.onclick = () => {
                  document.querySelectorAll('.sidebar-tab').forEach(t => t.classList.remove('active'));
                  document.querySelectorAll('.sidebar-tab-content').forEach(c => c.classList.remove('active'));
                  tab.classList.add('active');
                  document.getElementById(tab.getAttribute('data-target')).classList.add('active');
              };
          });
  
          // --- Render Check Groups ---
          function renderCheckGroups() {
              const container = document.getElementById('checkGroupsContainer');
              container.innerHTML = '';
  
              for (const [groupName, items] of Object.entries(checkCategories)) {
                  const group = document.createElement('div');
                  group.className = 'check-group'; // default collapsed
                  const gid = groupName.replace(/[^a-z]/gi, '_');
  
                  group.innerHTML = `
                      <div class="check-group-header" onclick="toggleGroup(this)">
                          <div class="check-group-title">
                              ${groupName}
                              <span class="counter" id="cnt_${gid}">${items.length}/${items.length}</span>
                          </div>
                          <div class="check-group-actions">
                              <span class="select-link" 
onclick="event.stopPropagation();selectAll('${gid}',true)">All</span>
                              <span class="select-link" 
onclick="event.stopPropagation();selectAll('${gid}',false)">None</span>
                              <span class="chevron">▼</span>
                          </div>
                      </div>
                      <div class="check-items" id="items_${gid}">
                          ${items.map(c => `
                              <div class="check-item-ui">
                                  <input type="checkbox" id="chk_${c.id}" class="check-opt" value="${c.id}" checked
                                          onchange="updateCounter('${gid}')">
                                  <label for="chk_${c.id}">${c.label}</label>
                              </div>
                          `).join('')}
                      </div>
                  `;
                  container.appendChild(group);
              }
          }
  
          function toggleGroup(header) {
              header.parentElement.classList.toggle('open');
          }
  
          function selectAll(gid, checked) {
              document.querySelectorAll(`#items_${gid} .check-opt`).forEach(c => c.checked = checked);
              updateCounter(gid);
          }
  
          function updateCounter(gid) {
              const all = document.querySelectorAll(`#items_${gid} .check-opt`);
              const checked = document.querySelectorAll(`#items_${gid} .check-opt:checked`);
              document.getElementById(`cnt_${gid}`).textContent = `${checked.length}/${all.length}`;
              if (event && event.type === 'change') {
                  triggerConfigSaveChecks();
              } else if (event && event.type === 'click') {
                  triggerConfigSaveChecks();
              }
          }
  
          function triggerConfigSaveChecks() {
              clearTimeout(saveTimeout);
              saveTimeout = setTimeout(() => {
                  const checked = Array.from(document.querySelectorAll('.check-opt:checked')).map(cb => cb.value);
                  configData.last_enabled_checks = checked;
                  
                  fetch('/api/config', {
                      method: 'POST',
                      headers: { 'Content-Type': 'application/json' },
                      body: JSON.stringify(configData)
                  });
              }, 600);
          }
  
          // Save config parameters
          saveConfigBtn.onclick = () => {
              saveConfigBtn.disabled = true;
              saveConfigBtn.textContent = "Saving...";
              
              Object.keys(cfgMap).forEach(k => {
                  const el = cfgMap[k];
                  if (el.type === 'number') {
                      configData[k] = el.step ? parseFloat(el.value) : parseInt(el.value);
                      if (isNaN(configData[k])) configData[k] = undefined;
                  } else {
                      configData[k] = el.value;
                  }
              });
              
              if(!configData.thresholds) configData.thresholds = {};
              Object.keys(thrMap).forEach(k => {
                  const el = thrMap[k];
                  if (el.type === 'number') {
                      configData.thresholds[k] = el.step ? parseFloat(el.value) : parseInt(el.value);
                      if (isNaN(configData.thresholds[k])) configData.thresholds[k] = undefined;
                  } else {
                      configData.thresholds[k] = el.value;
                  }
              });
  
              fetch('/api/config', {
                  method: 'POST',
                  headers: { 'Content-Type': 'application/json' },
                  body: JSON.stringify(configData)
              })
              .then(r => r.json())
              .then(data => {
                  saveConfigBtn.disabled = false;
                  saveConfigBtn.textContent = "Save Config";
                  if(data.status === 'success') {
                      configMsg.style.display = 'block';
                      setTimeout(() => { configMsg.style.display = 'none'; }, 3000);
                  }
              });
          };
  
          // Filter Logic
          filterBtns.forEach(btn => {
              btn.onclick = (e) => {
                  filterBtns.forEach(b => b.classList.remove('active'));
                  btn.classList.add('active');
                  currentFilter = btn.getAttribute('data-filter');
                  applyFilter();
              };
          });
  
          function applyFilter() {
              document.querySelectorAll('.check-item').forEach(el => {
                  const status = el.getAttribute('data-status');
                  let show = true;
                  if (currentFilter === 'error' && status !== 'fail' && status !== 'error') show = false;
                  if (currentFilter === 'warn' && status !== 'warn') show = false;
                  if (currentFilter === 'pass' && status !== 'pass') show = false;
                  
                  if (show) { el.classList.remove('hidden-by-filter'); }
                  else { el.classList.add('hidden-by-filter'); }
              });
  
              document.querySelectorAll('.section-box').forEach(section => {
                  const visibleChecks = section.querySelectorAll('.check-item:not(.hidden-by-filter)').length;
                  if (visibleChecks === 0 && currentFilter !== 'all') {
                      section.classList.add('hidden-by-filter');
                  } else {
                      section.classList.remove('hidden-by-filter');
                  }
              });
          }
  
  
          // --- WS Handling ---
          ws.onmessage = (e) => {
              const m = JSON.parse(e.data);
              if (m.type === 'run_state') handleRunState(m);
              if (m.type === 'cluster_status') updateStatus(m.cluster, m.status);
              if (m.type === 'headline') updateHeadline(m.cluster, m.message);
              if (m.type === 'result') addResult(m.cluster, m.data);
              if (m.type === 'error') showError(m.cluster ? `${m.cluster}: ${m.message}` : m.message);
              if (m.type === 'report') addReportPath(m.cluster, m.path);
              if (m.type === 'reports_ready') showCombinedReport(m.path, m.count);
              if (m.type === 'complete') {
                  updateStatus(m.cluster, m.summary.overall_status);
                  updateHeadline(m.cluster, "Check sequence complete.");
              }
          };
  
          function escapeHtml(value) {
              return String(value ?? '').replaceAll('&', '&amp;').replaceAll('<', '&lt;').replaceAll('>', 
'&gt;').replaceAll('"', '&quot;').replaceAll("'", '&#39;');
          }
  
          function resetDashboard() {
              cardsContainer.innerHTML = '';
              clusters = {};
              stats = { pass: 0, warn: 0, fail: 0 };
              updateScoreboard();
              errorBanner.style.display = 'none';
              errorBanner.textContent = '';
              reportBanner.style.display = 'none';
              reportBanner.textContent = '';
              filterBtns.forEach(b => b.classList.remove('active'));
              document.querySelector('.filter-btn[data-filter="all"]').classList.add('active');
              currentFilter = 'all';
          }
  
          function updateScoreboard() {
              document.getElementById('totalPass').textContent = stats.pass;
              document.getElementById('totalWarn').textContent = stats.warn;
              document.getElementById('totalFail').textContent = stats.fail;
          }
  
          function handleRunState(message) {
              if (message.state === 'started') {
                  isRunning = true;
                  resetDashboard();
                  runBtn.disabled = true;
                  runBtn.innerHTML = '⚙️ Diagnostics Running...';
                  return;
              }
              if (message.state === 'finished') {
                  isRunning = false;
                  runBtn.disabled = false;
                  runBtn.innerHTML = '🚀 Run Health Check';
                  applyFilter();
              }
          }
  
          function normalizeStatus(status) {
              const normalized = String(status || '').toLowerCase();
              if (['idle', 'running', 'pass', 'fail', 'warn', 'error', 'connecting', 'pushing 
backend'].includes(normalized)) {
                  return normalized.replace(' ', '-');
              }
              return 'running';
          }
  
          function showError(message) {
              errorBanner.style.display = 'block';
              errorBanner.textContent = message;
              if (!isRunning) {
                  runBtn.disabled = false;
                  runBtn.innerHTML = '🚀 Run Health Check';
              }
          }
  
          function updateStatus(name, status) {
              const card = getOrCreateCard(name);
              const pill = card.querySelector('.status-pill');
              pill.textContent = status;
              pill.className = `status-pill status-${normalizeStatus(status)}`;
          }
  
          function updateHeadline(name, msg) {
              const card = getOrCreateCard(name);
              card.querySelector('.headline-zone').textContent = "> " + msg;
          }
  
          function addResult(name, data) {
              const card = getOrCreateCard(name);
              const container = card.querySelector('.details-area');
              
              const section = document.createElement('div');
              section.className = 'section-box';
              
              const sectionStatus = (data.status || '').toLowerCase();
              if (sectionStatus === 'fail' || sectionStatus === 'error') stats.fail++;
              else if (sectionStatus === 'warn') stats.warn++;
              else stats.pass++;
              updateScoreboard();
  
              const statusClass = sectionStatus === 'fail' || sectionStatus === 'error' ? 'text-fail' : sectionStatus 
=== 'warn' || sectionStatus === 'skip' ? 'text-warn' : 'text-pass';
              
              section.innerHTML = `
                  <div class="section-header">
                      <span>${escapeHtml(data.name)} <span style="font-size: 0.8rem; color: var(--text-dim); 
font-weight: normal;">(${escapeHtml(data.category)})</span></span>
                      <span class="${statusClass}">${sectionStatus.toUpperCase()}</span>
                  </div>
                  <div class="section-content">
                      ${data.checks.map(c => {
                          const cStatus = (c.status||'').toLowerCase();
                          return \`
                          <div class="check-item" data-status="\${cStatus}">
                              <span class="check-status text-\${cStatus}">[\${escapeHtml(c.status)}]</span>
                              <div style="flex: 1;">
                                  <div style="font-weight: 500;">\${escapeHtml(c.message)}</div>
                                  \${c.detail ? \`<div style="font-family: monospace; font-size:0.8rem; 
color:var(--text-dim); margin-top:0.4rem; padding:0.5rem; background:rgba(0,0,0,0.3); border-radius:4px; 
white-space:pre-wrap; word-break:break-all;">\${escapeHtml(c.detail)}</div>\` : ''}
                              </div>
                          </div>
                          \`;
                      }).join('')}
                  </div>
              `;
  
              section.querySelector('.section-header').onclick = () => {
                  section.querySelector('.section-content').classList.toggle('open');
              };
  
              container.appendChild(section);
              applyFilter();
          }
  
          function addReportPath(name, path) {}
  
          function showCombinedReport(path, count) {
              reportBanner.innerHTML = `<strong>Diagnostic Complete!</strong> Generated combined report for ${count} 
cluster(s).<br><span style="font-size:0.8rem; font-family:monospace; margin-top:0.5rem; 
display:block;">${path}</span><br><a href="/report/latest" target="_blank" class="btn btn-outline" 
style="margin-top:0.5rem; padding: 0.4rem 0.8rem; font-size: 0.8rem;">View HTML Report</a>`;
              reportBanner.style.display = 'block';
          }
  
          function getOrCreateCard(name) {
              if (clusters[name]) return clusters[name];
              
              const div = document.createElement('div');
              div.className = 'glass-panel cluster-card';
              div.innerHTML = `
                  <div class="card-header">
                      <div class="cluster-info">
                          <h2>${escapeHtml(name)}</h2>
                      </div>
                      <span class="status-pill status-idle">Idle</span>
                  </div>
                  <div class="headline-zone">> Awaiting start...</div>
                  <div style="display: flex; justify-content: flex-end;">
                      <button class="btn btn-outline" style="padding: 0.4rem 1rem; font-size: 0.8rem;">Expand 
Logs</button>
                  </div>
                  <div class="details-area"></div>
              `;
  
              div.querySelector('.btn-outline').onclick = (e) => {
                  const details = div.querySelector('.details-area');
                  details.classList.toggle('open');
                  e.target.textContent = details.classList.contains('open') ? 'Collapse Logs' : 'Expand Logs';
              };
  
              cardsContainer.appendChild(div);
              clusters[name] = div;
              return div;
          }
  
          // Run Action
          runBtn.onclick = () => {
              if (isRunning) return;
              if (ws.readyState !== WebSocket.OPEN) {
                  showError('WebSocket connection is not available. Please restart the backend.');
                  return;
              }
              
              const enabledChecks = Array.from(document.querySelectorAll('.check-opt:checked')).map(cb => cb.value);
  
              errorBanner.style.display = 'none';
              errorBanner.textContent = '';
              
              ws.send(JSON.stringify({ 
                  action: "start_all",
                  enabled_checks: enabledChecks
              }));
              
              runBtn.disabled = true;
          };
  
          // Boot
          init();
      </script>
  </body>
  </html>



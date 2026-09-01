import { apiCall } from './auth.js';
import { showToast, escapeHtml } from './utils.js';
import { t } from './i18n.js';

let originalSettings = {};
let modelMappings = {};

// 双引号属性上下文专用转义：escapeHtml(textContent 实现) 不会转义 ASCII 双引号，
// 直接拼入 value="..."/data-*="..." 仍可被 `"` 逸出注入事件属性，故额外转义双引号。
function escapeAttr(str) {
  return escapeHtml(String(str ?? '')).replace(/"/g, '&quot;');
}

function getGroupTitle(groupKey) {
  const map = {
    performance: 'settings.group.performance',
    rate_limiting: 'settings.group.rateLimit',
    health_check: 'settings.group.healthCheck',
    account_management: 'settings.group.accounts',
    usage_stats: 'settings.group.stats',
    chat_cleanup: 'settings.group.chatCleanup',
    logging: 'settings.group.logging'
  };
  return t(map[groupKey] || groupKey);
}

const GROUP_ICONS = {
  performance: 'fa-bolt',
  rate_limiting: 'fa-shield-alt',
  health_check: 'fa-heartbeat',
  account_management: 'fa-users-cog',
  usage_stats: 'fa-chart-line',
  chat_cleanup: 'fa-broom',
  logging: 'fa-file-alt'
};

function getFieldLabel(key) {
  const map = {
    refresh_interval: 'settings.field.refreshInterval',
    max_retries: 'settings.field.maxRetries',
    jitter_enabled: 'settings.field.jitterEnabled',
    rate_limit_enabled: 'settings.field.rateLimitEnabled',
    rate_limit_window: 'settings.field.rateLimitWindow',
    rate_limit_max: 'settings.field.rateLimitMax',
    health_check_enabled: 'settings.field.healthCheckEnabled',
    health_check_interval: 'settings.field.healthCheckInterval',
    rotation_strategy: 'settings.field.rotationStrategy',
    max_concurrent_per_account: 'settings.field.maxConcurrent',
    usage_stats_enabled: 'settings.field.usageStatsEnabled',
    usage_stats_interval: 'settings.field.usageStatsInterval',
    usage_stats_retention_days: 'settings.field.usageStatsRetention',
    version_sync_enabled: 'settings.field.versionSyncEnabled',
    extended_thinking_enabled: 'settings.field.extendedThinkingEnabled',
    chat_cleanup_enabled: 'settings.field.chatCleanupEnabled',
    chat_cleanup_keep_hours: 'settings.field.chatCleanupKeepHours',
    chat_cleanup_interval_hours: 'settings.field.chatCleanupInterval',
    chat_cleanup_skip_pinned: 'settings.field.chatCleanupSkipPinned',
    log_bodies_enabled: 'settings.field.logBodiesEnabled'
  };
  return map[key] ? (map[key].startsWith('settings.') ? t(map[key]) : map[key]) : key;
}

// 字段级补充说明（可选）。只有出现在这里的字段才会多渲染一行提示，
// 未登记的字段渲染结果与之前逐字节一致。
const FIELD_HINTS = {
  log_bodies_enabled: 'settings.hint.logBodies'
};

function getFieldHint(key) {
  return FIELD_HINTS[key] ? t(FIELD_HINTS[key]) : '';
}

const ROTATION_OPTIONS = [
  { value: 'round-robin', labelKey: 'settings.strategy.roundRobin' },
  { value: 'failover', labelKey: 'settings.strategy.failover' }
];

function createFieldInput(key, value) {
  const type = typeof value;

  if (type === 'boolean') {
    return `
      <label class="toggle-switch">
        <input type="checkbox" data-key="${key}" ${value ? 'checked' : ''}>
      </label>
    `;
  }

  if (key.endsWith('.rotation_strategy')) {
    const radios = ROTATION_OPTIONS.map(opt =>
      `<label class="radio-option">
        <input type="radio" name="rotation_strategy" data-key="${key}" value="${opt.value}" ${value === opt.value ? 'checked' : ''}>
        <span>${t(opt.labelKey)}</span>
      </label>`
    ).join('');
    return `<div class="radio-group">${radios}</div>`;
  }

  if (type === 'number') {
    // step 既是给浏览器的输入约束，也是 collectFormValues 判断该用 parseInt 还是
    // parseFloat 的依据：后端把 chat_cleanup_* 声明为 float，允许 0.5 这样的小时数，
    // 一律 parseInt 会把它静默截断成 0。整数字段仍是 step=1，行为与之前完全一致。
    const step = Number.isInteger(value) ? '1' : 'any';
    return `<input type="number" class="form-control" data-key="${key}" step="${step}" value="${value}">`;
  }

  return `<input type="text" class="form-control" data-key="${key}" value="${value}">`;
}

function renderSettings(settings) {
  const container = document.getElementById('settings-container');
  if (!container) return;

  let html = '';

  for (const [groupKey, groupSettings] of Object.entries(settings)) {
    const groupTitle = getGroupTitle(groupKey);
    const groupIcon = GROUP_ICONS[groupKey] || 'fa-cog';

    html += '<div class="settings-group">';
    html += '<h3><i class="fas ' + groupIcon + '"></i> ' + groupTitle + '</h3>';
    html += '<div class="settings-fields">';

    for (const [key, value] of Object.entries(groupSettings)) {
      const label = getFieldLabel(key);
      const fullKey = groupKey + '.' + key;
      const input = createFieldInput(fullKey, value);
      const hint = getFieldHint(key);
      html += '<div class="setting-field"><label>' + label + '</label>' + input +
        (hint ? '<small class="form-hint">' + escapeHtml(hint) + '</small>' : '') + '</div>';
    }

    html += '</div></div>';
  }

  container.innerHTML = html;
}

function renderModelMapping() {
  const container = document.getElementById('model-mapping-container');
  if (!container) return;

  const entries = Object.entries(modelMappings);

  let html = '<div class="settings-group">';
  html += '<h3><i class="fas fa-exchange-alt"></i> <span data-i18n="settings.modelMapping">' + t('settings.modelMapping') + '</span></h3>';
  html += '<p class="mapping-desc" data-i18n="settings.modelMappingDesc">' + t('settings.modelMappingDesc') + '</p>';
  html += '<div class="mapping-header"><span data-i18n="settings.requestModel">' + t('settings.requestModel') + '</span><span data-i18n="settings.actualModel">' + t('settings.actualModel') + '</span><span></span></div>';

  for (const [alias, target] of entries) {
    // 安全：alias/target 为用户可控且持久化（POST /admin/model-mapping 不做 HTML 过滤），
    // 拼入 innerHTML 前一律 escapeHtml 转义；属性值与 data-alias 同样转义，
    // 既防止属性逸出型存储 XSS，也避免引号污染 data-alias 导致 save/delete 取错别名。
    const aliasEsc = escapeAttr(alias);
    const targetEsc = escapeAttr(target);
    html += '<div class="mapping-row" data-alias="' + aliasEsc + '">';
    html += '<input type="text" class="form-control mapping-alias" value="' + aliasEsc + '" readonly>';
    html += '<input type="text" class="form-control mapping-target" value="' + targetEsc + '">';
    html += '<button class="btn-icon btn-delete-mapping" data-alias="' + aliasEsc + '"><i class="fas fa-trash"></i></button>';
    html += '</div>';
  }

  html += '<div class="mapping-row mapping-new">';
  html += '<input type="text" class="form-control" id="new-mapping-alias" data-i18n-placeholder="settings.aliasPlaceholder" placeholder="' + t('settings.aliasPlaceholder') + '">';
  html += '<input type="text" class="form-control" id="new-mapping-target" data-i18n-placeholder="settings.targetPlaceholder" placeholder="' + t('settings.targetPlaceholder') + '">';
  html += '<button class="btn-icon btn-add-mapping" id="btn-add-mapping"><i class="fas fa-plus"></i></button>';
  html += '</div>';
  html += '</div>';
  html += '<div class="mapping-actions">';
  html += '<button class="btn btn-primary" id="btn-save-mapping"><span data-i18n="settings.saveMapping">' + t('settings.saveMapping') + '</span></button>';
  html += '</div>';
  html += '</div>';

  container.innerHTML = html;
  bindMappingEvents();
}

function bindMappingEvents() {
  document.getElementById('btn-add-mapping')?.addEventListener('click', addMapping);
  document.getElementById('btn-save-mapping')?.addEventListener('click', saveAllMappings);
  document.querySelectorAll('.btn-delete-mapping').forEach(btn => {
    btn.addEventListener('click', () => deleteMapping(btn.dataset.alias));
  });
  document.getElementById('new-mapping-target')?.addEventListener('keydown', (e) => {
    if (e.key === 'Enter') addMapping();
  });
}

async function addMapping() {
  const aliasInput = document.getElementById('new-mapping-alias');
  const targetInput = document.getElementById('new-mapping-target');
  const alias = aliasInput.value.trim();
  const target = targetInput.value.trim();

  if (!alias || !target) {
    showToast('请填写别名和目标模型', 'warning');
    return;
  }
  if (alias === target) {
    showToast('别名不能与目标相同', 'warning');
    return;
  }

  try {
    await apiCall('POST', '/admin/model-mapping', { alias, target });
    showToast('映射已添加', 'success');
    await loadModelMapping();
  } catch (error) {
    showToast('添加失败: ' + error.message, 'error');
  }
}

async function deleteMapping(alias) {
  try {
    await apiCall('DELETE', '/admin/model-mapping/' + encodeURIComponent(alias));
    showToast('映射已删除', 'success');
    await loadModelMapping();
  } catch (error) {
    showToast('删除失败: ' + error.message, 'error');
  }
}

async function saveAllMappings() {
  const rows = document.querySelectorAll('.mapping-row:not(.mapping-new)');
  let updated = 0;

  for (const row of rows) {
    const alias = row.dataset.alias;
    const target = row.querySelector('.mapping-target').value.trim();
    if (target && target !== modelMappings[alias]) {
      try {
        await apiCall('POST', '/admin/model-mapping', { alias, target });
        updated++;
      } catch (error) {
        showToast('更新 ' + alias + ' 失败: ' + error.message, 'error');
      }
    }
  }

  if (updated > 0) {
    showToast('已更新 ' + updated + ' 条映射', 'success');
  } else {
    showToast('无变更', 'info');
  }
  await loadModelMapping();
}

async function loadModelMapping() {
  try {
    const data = await apiCall('GET', '/admin/model-mapping');
    modelMappings = data.mappings || {};
    renderModelMapping();
  } catch (error) {
    showToast('加载模型映射失败: ' + error.message, 'error');
  }
}

function collectFormValues() {
  const values = {};

  document.querySelectorAll('#settings-container [data-key]').forEach(input => {
    const key = input.dataset.key;

    if (input.type === 'radio') {
      if (input.checked) {
        values[key] = input.value;
      }
      return;
    }

    let value;
    if (input.type === 'checkbox') {
      value = input.checked;
    } else if (input.type === 'number') {
      // step="any" 的字段（后端声明为 float）必须用 parseFloat，否则 0.5 会被截断成 0
      // 并连带整个保存请求一起被后端拒掉。
      value = input.step === 'any' ? parseFloat(input.value) : parseInt(input.value, 10);
    } else {
      value = input.value;
    }

    values[key] = value;
  });

  return values;
}

function getChangedSettings(current, original) {
  const changed = {};
  for (const [key, value] of Object.entries(current)) {
    if (original[key] !== value) {
      changed[key] = value;
    }
  }
  return changed;
}

function flattenSettings(settings) {
  const flat = {};
  for (const [groupKey, groupSettings] of Object.entries(settings)) {
    for (const [key, value] of Object.entries(groupSettings)) {
      flat[groupKey + '.' + key] = value;
    }
  }
  return flat;
}

export async function loadSettings() {
  try {
    const data = await apiCall('GET', '/admin/settings');
    originalSettings = flattenSettings(data);
    renderSettings(data);
    await loadModelMapping();
  } catch (error) {
    showToast('加载设置失败: ' + error.message, 'error');
  }
}

async function saveSettings() {
  const currentValues = collectFormValues();
  const changedSettings = getChangedSettings(currentValues, originalSettings);

  if (Object.keys(changedSettings).length === 0) {
    showToast('没有修改', 'info');
    return;
  }

  const apiSettings = {};
  for (const [key, value] of Object.entries(changedSettings)) {
    const fieldName = key.split('.').pop();
    apiSettings[fieldName] = value;
  }

  try {
    await apiCall('POST', '/admin/settings', { settings: apiSettings });
    showToast(t('settings.saved'), 'success');
    await loadSettings();
  } catch (error) {
    showToast(t('settings.saveFailed') + ': ' + error.message, 'error');
  }
}

async function resetSettings() {
  await loadSettings();
  showToast(t('settings.resetDone'), 'info');
}

export function initSettings() {
  const saveBtn = document.getElementById('settings-save-btn');
  const resetBtn = document.getElementById('settings-reset-btn');

  if (saveBtn) {
    saveBtn.addEventListener('click', saveSettings);
  }

  if (resetBtn) {
    resetBtn.addEventListener('click', resetSettings);
  }
}

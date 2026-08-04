import { apiCall } from './auth.js';
import { showToast } from './utils.js';

let timer = null;
let isRunning = false;

const text = (id, value) => { const el = document.getElementById(id); if (el) el.textContent = value; };
const escapeHtml = value => String(value ?? '').replace(/[&<>'"]/g, c => ({'&':'&amp;','<':'&lt;','>':'&gt;',"'":'&#39;','"':'&quot;'}[c]));
const localTime = value => value ? new Date(value).toLocaleString('zh-CN', { hour12: false }) : '—';
const duration = ms => ms == null ? '—' : ms < 1000 ? `${ms} ms` : `${(ms / 1000).toFixed(2)} s`;

function render(data) {
    const { config, stats, current, history, running, next_run_at: next } = data;
    document.getElementById('patrol-enabled').checked = config.enabled;
    document.getElementById('patrol-interval').value = config.interval_minutes;
    document.getElementById('patrol-text-enabled').checked = config.text_test_enabled;
    document.getElementById('patrol-image-enabled').checked = config.image_test_enabled;
    document.getElementById('patrol-model').value = config.model;
    document.getElementById('patrol-notify-enabled').checked = config.notify_enabled;
    text('patrol-webhook-state', config.webhook_configured ? `Webhook 已配置${config.secret_configured ? ' · 校验已开启' : ''}` : '尚未配置 Webhook');

    text('patrol-today-tasks', stats.today.tasks);
    text('patrol-today-detail', `${stats.today.rounds} 轮 · ${stats.today.success} 成功`);
    text('patrol-current-tasks', stats.current.tasks);
    text('patrol-current-detail', `${stats.current.success} 成功 · ${stats.current.failed} 失败`);
    text('patrol-history-tasks', stats.history.tasks);
    text('patrol-history-detail', `${stats.history.rounds} 轮 · ${stats.history.success} 成功`);

    const livebar = document.getElementById('patrol-livebar');
    isRunning = running;
    livebar.classList.toggle('is-running', running);
    text('patrol-state', running ? `正在执行 ${current?.id || ''}` : config.enabled ? '自动盘巡已开启' : '自动盘巡已暂停');
    text('patrol-next-run', running ? `${current?.success || 0}/${current?.total || 0} 已完成` : next ? `下一轮 ${localTime(next)}` : '可手动执行');
    const runBtn = document.getElementById('patrol-run-btn');
    runBtn.disabled = running;
    runBtn.innerHTML = running ? '<i class="fas fa-spinner fa-spin"></i> 盘巡进行中' : '<i class="fas fa-play"></i> 立即执行一轮';

    document.getElementById('patrol-history').innerHTML = history.length ? history.map(renderRound).join('') : '<div class="patrol-empty">暂无盘巡记录</div>';
}

function renderRound(round) {
    const statusMap = { success: '全部通过', partial: '存在失败', failed: '执行失败', empty: '无任务', cancelled: '已取消', running: '执行中' };
    const tasks = (round.tasks || []).map(task => `
        <div class="patrol-task">
            <b title="${escapeHtml(task.account_id)}">${escapeHtml(task.account_label)}</b>
            <span>${task.type === 'image' ? `图文 #${escapeHtml(task.image_sample || '-')}` : '文字'}</span>
            <span>${duration(task.duration_ms)}</span>
            <span class="patrol-task-response ${task.success ? 'patrol-ok' : 'patrol-bad'}" title="${escapeHtml(task.response_preview || task.error)}">${task.success ? escapeHtml(task.response_preview || '成功') : escapeHtml(task.error || '失败')}</span>
        </div>`).join('');
    const notify = round.notification?.sent ? ' · 飞书已通知' : round.notification?.error ? ' · 飞书通知失败' : '';
    return `<details class="patrol-round">
        <summary>
            <span class="patrol-round-title"><b>${escapeHtml(round.id)}</b><small>${localTime(round.started_at)} · ${round.trigger === 'scheduled' ? '定时' : '手动'} · ${duration(round.duration_ms)}${notify}</small></span>
            <span class="patrol-round-score">${round.success || 0}/${round.total || 0}</span>
            <span class="patrol-pill ${escapeHtml(round.status)}">${statusMap[round.status] || escapeHtml(round.status)}</span>
        </summary>
        <div class="patrol-tasks">${tasks || '<div class="patrol-empty">本轮没有任务</div>'}</div>
    </details>`;
}

export async function loadPatrol() {
    if (!document.getElementById('patrol')) return;
    try {
        render(await apiCall('GET', '/admin/patrol'));
    } catch (error) {
        showToast(`加载盘巡数据失败：${error.message}`, 'error');
    }
}

async function saveConfig(event) {
    event.preventDefault();
    const body = {
        enabled: document.getElementById('patrol-enabled').checked,
        interval_minutes: Number(document.getElementById('patrol-interval').value),
        text_test_enabled: document.getElementById('patrol-text-enabled').checked,
        image_test_enabled: document.getElementById('patrol-image-enabled').checked,
        model: document.getElementById('patrol-model').value,
        notify_enabled: document.getElementById('patrol-notify-enabled').checked,
        webhook_url: document.getElementById('patrol-webhook').value.trim(),
        webhook_secret: document.getElementById('patrol-secret').value.trim(),
    };
    try {
        await apiCall('PUT', '/admin/patrol/config', body);
        document.getElementById('patrol-webhook').value = '';
        document.getElementById('patrol-secret').value = '';
        showToast('盘巡设置已保存', 'success');
        await loadPatrol();
    } catch (error) {
        showToast(`保存失败：${error.message}`, 'error');
    }
}

async function runRound() {
    try {
        await apiCall('POST', '/admin/patrol/run');
        showToast('新一轮盘巡已启动', 'success');
        await loadPatrol();
    } catch (error) {
        showToast(`启动失败：${error.message}`, 'error');
    }
}

export function initPatrol() {
    document.getElementById('patrol-config-form')?.addEventListener('submit', saveConfig);
    document.getElementById('patrol-run-btn')?.addEventListener('click', runRound);
    document.getElementById('patrol-refresh-btn')?.addEventListener('click', loadPatrol);
    clearInterval(timer);
    timer = setInterval(() => {
        if (isRunning && document.getElementById('patrol')?.classList.contains('active')) loadPatrol();
    }, 3000);
}

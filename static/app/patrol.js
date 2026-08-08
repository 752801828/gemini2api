import { apiCall, getToken } from './auth.js';
import { showToast } from './utils.js';

let timer = null;
let isRunning = false;
const thumbnailUrls = new Map();
const selectedRoundIds = new Set();

const text = (id, value) => { const el = document.getElementById(id); if (el) el.textContent = value; };
const escapeHtml = value => String(value ?? '').replace(/[&<>'"]/g, c => ({'&':'&amp;','<':'&lt;','>':'&gt;',"'":'&#39;','"':'&quot;'}[c]));
const localTime = value => value ? new Date(value).toLocaleString('zh-CN', { hour12: false }) : '—';
const duration = ms => ms == null ? '—' : ms < 1000 ? `${ms} ms` : `${(ms / 1000).toFixed(2)} s`;

function render(data) {
    const { config, stats, current, history, images, running, next_run_at: next } = data;
    document.getElementById('patrol-enabled').checked = config.enabled;
    document.getElementById('patrol-interval').value = config.interval_minutes;
    document.getElementById('patrol-text-enabled').checked = config.text_test_enabled;
    document.getElementById('patrol-image-enabled').checked = config.image_test_enabled;
    document.getElementById('patrol-text-count').value = config.text_test_count || 1;
    document.getElementById('patrol-image-count').value = config.image_test_count || 1;
    document.getElementById('patrol-image-min').value = config.image_min_count;
    document.getElementById('patrol-image-max').value = config.image_max_count;
    document.querySelectorAll('#patrol-models input').forEach(input => {
        input.checked = (config.models || ['gemini-flash']).includes(input.value);
    });
    document.getElementById('patrol-notify-enabled').checked = config.notify_enabled;
    text('patrol-webhook-state', config.webhook_configured ? `Webhook 已配置${config.secret_configured ? ' · 校验已开启' : ''}` : '尚未配置 Webhook');

    text('patrol-today-tasks', stats.today.tasks);
    text('patrol-today-detail', `${stats.today.rounds} 轮 · ${stats.today.success} 成功`);
    text('patrol-current-tasks', stats.current.tasks);
    text('patrol-current-detail', `${stats.current.success} 成功 · ${stats.current.failed} 失败`);
    text('patrol-history-tasks', stats.history.tasks);
    text('patrol-history-detail', `${stats.history.rounds} 轮 · ${stats.history.success} 成功`);
    const textStats = stats.types?.text || { tasks: 0, success: 0, rate: 0 };
    const imageStats = stats.types?.image || { tasks: 0, success: 0, rate: 0 };
    text('patrol-text-rate', `${textStats.rate}%`);
    text('patrol-text-rate-detail', `${textStats.success} / ${textStats.tasks} 成功 · 均耗时 ${duration(textStats.avg_duration_ms || 0)}`);
    text('patrol-image-rate', `${imageStats.rate}%`);
    text('patrol-image-rate-detail', `${imageStats.success} / ${imageStats.tasks} 成功 · 均耗时 ${duration(imageStats.avg_duration_ms || 0)}`);

    const livebar = document.getElementById('patrol-livebar');
    isRunning = running;
    livebar.classList.toggle('is-running', running);
    text('patrol-state', running ? `正在执行 ${current?.id || ''}` : config.enabled ? '自动盘巡已开启' : '自动盘巡已暂停');
    text('patrol-next-run', running ? `${current?.success || 0}/${current?.total || 0} 已完成` : next ? `下一轮 ${localTime(next)}` : '可手动执行');
    const runBtn = document.getElementById('patrol-run-btn');
    runBtn.disabled = running;
    runBtn.innerHTML = running ? '<i class="fas fa-spinner fa-spin"></i> 盘巡进行中' : '<i class="fas fa-play"></i> 立即执行一轮';

    const imageLookup = new Map((images || []).map(image => [image.name, image.id]));
    renderImages(images || []);
    const visibleRoundIds = new Set(history.map(round => String(round.id)));
    [...selectedRoundIds].forEach(roundId => { if (!visibleRoundIds.has(roundId)) selectedRoundIds.delete(roundId); });
    document.getElementById('patrol-history').innerHTML = history.length ? history.map((round, index) => renderRound(round, imageLookup, index)).join('') : '<div class="patrol-empty">暂无盘巡记录</div>';
    updateRoundSelectionControls();
    hydrateImageThumbs();
}

function formatBytes(bytes) {
    return bytes < 1024 * 1024 ? `${Math.ceil(bytes / 1024)} KB` : `${(bytes / 1024 / 1024).toFixed(1)} MB`;
}

function renderImages(images) {
    text('patrol-image-total', `${images.length} 张`);
    const library = document.getElementById('patrol-image-library');
    library.innerHTML = images.length ? images.map(image => `
        <article class="patrol-image-card">
            <img data-patrol-image="${escapeHtml(image.id)}" alt="${escapeHtml(image.name)}">
            <button class="patrol-image-delete" type="button" data-delete-image="${escapeHtml(image.id)}" aria-label="删除 ${escapeHtml(image.name)}"><i class="fas fa-trash"></i></button>
            <div class="patrol-image-meta"><b title="${escapeHtml(image.name)}">${escapeHtml(image.name)}</b><small>${formatBytes(image.size)}</small></div>
        </article>`).join('') : '<div class="patrol-library-empty">还没有图片，上传后才能执行图文测试</div>';
}

async function hydrateImageThumbs() {
    const token = getToken();
    document.querySelectorAll('[data-patrol-image]').forEach(async image => {
        if (thumbnailUrls.has(image.dataset.patrolImage)) {
            image.src = thumbnailUrls.get(image.dataset.patrolImage);
            return;
        }
        try {
            const response = await fetch(`/admin/patrol/images/${encodeURIComponent(image.dataset.patrolImage)}`, {
                headers: { Authorization: `Bearer ${token}` }
            });
            if (!response.ok) return;
            const url = URL.createObjectURL(await response.blob());
            thumbnailUrls.set(image.dataset.patrolImage, url);
            image.src = url;
        } catch (_) {}
    });
}

function renderRound(round, imageLookup, roundIndex) {
    const statusMap = { success: '全部通过', partial: '存在失败', failed: '执行失败', empty: '无任务', cancelled: '已取消', running: '执行中' };
    const tasks = (round.tasks || []).map((task, taskIndex) => {
        const imageNames = task.image_samples || (task.image_sample ? [task.image_sample] : []);
        const imageIds = task.image_sample_ids || [];
        const imageItems = imageNames.map((name, index) => ({ name, id: imageIds[index] || imageLookup.get(name) || '' }));
        const typeLabel = task.type === 'image' ? '图文' : '文字';
        const resultText = task.success ? task.response_preview || '成功' : task.error || '失败';
        const detailText = task.success ? task.response || task.response_preview || '成功' : task.error || '失败';
        const legacyPreview = task.success && !task.response && (task.response_preview || '').length >= 300;
        return `
        <details class="patrol-task-detail">
            <summary class="patrol-task">
                <span class="patrol-task-index">任务 ${String(taskIndex + 1).padStart(2, '0')}</span>
                <b title="${escapeHtml(task.account_id)}">${escapeHtml(task.account_label)}</b>
                <span title="${escapeHtml(imageNames.join('、'))}">${typeLabel} #${task.sequence || 1}${task.type === 'image' ? ` · ${imageNames.length} 张` : ''}</span>
                <span>${escapeHtml(task.model || '-')}</span>
                <span>${duration(task.duration_ms)}</span>
                <span class="patrol-task-response ${task.success ? 'patrol-ok' : 'patrol-bad'}">${escapeHtml(resultText)}</span>
                <i class="fas fa-chevron-down patrol-task-chevron"></i>
            </summary>
            <div class="patrol-task-info">
                <div class="patrol-task-toolbar"><b>${typeLabel}任务详情</b></div>
                <div class="patrol-task-field"><span>任务</span><p>${typeLabel}测试 #${task.sequence || 1}</p></div>
                <div class="patrol-task-field"><span>模型</span><p>${escapeHtml(task.model || '-')}</p></div>
                <div class="patrol-task-field"><span>状态 / 耗时</span><p class="${task.success ? 'patrol-ok' : 'patrol-bad'}">${task.success ? '成功' : '失败'} · ${duration(task.duration_ms)}</p></div>
                <div class="patrol-task-field wide"><span>随机问题</span><p>${escapeHtml(task.prompt || '-')}</p></div>
                ${imageItems.length ? `<div class="patrol-task-field wide"><span>选中图片（${imageItems.length} 张）</span><div class="patrol-task-images">${imageItems.map(image => image.id ? `<figure><img data-patrol-image="${escapeHtml(image.id)}" alt="${escapeHtml(image.name)}"><figcaption>${escapeHtml(image.name)}</figcaption></figure>` : `<div class="patrol-task-image-missing"><i class="fas fa-image"></i>${escapeHtml(image.name)}</div>`).join('')}</div></div>` : ''}
                <div class="patrol-task-field wide"><span>${task.success ? `模型响应（${detailText.length} 字）` : '错误信息'}</span><pre class="patrol-task-full-response">${escapeHtml(detailText)}</pre>${legacyPreview ? '<small class="patrol-response-note">此旧任务当时只保存了 300 字预览；新任务会完整保存。</small>' : ''}</div>
            </div>
        </details>`;
    }).join('');
    const notify = round.notification?.sent ? ' · 飞书已通知' : round.notification?.error ? ' · 飞书通知失败' : '';
    return `<details class="patrol-round">
        <summary>
            <input class="patrol-round-select" type="checkbox" data-select-round="${escapeHtml(round.id)}" aria-label="选择轮次 ${escapeHtml(round.id)}"${selectedRoundIds.has(String(round.id)) ? ' checked' : ''}>
            <span class="patrol-round-mark">轮次 ${String(roundIndex + 1).padStart(2, '0')}</span>
            <span class="patrol-round-title"><b>${escapeHtml(round.id)}</b><small>${localTime(round.started_at)} · ${round.trigger === 'scheduled' ? '定时' : '手动'} · ${duration(round.duration_ms)}${notify}</small></span>
            <span class="patrol-round-score">${round.success || 0}/${round.total || 0}</span>
            <span class="patrol-pill ${escapeHtml(round.status)}">${statusMap[round.status] || escapeHtml(round.status)}</span>
        </summary>
        <div class="patrol-tasks"><div class="patrol-task-list-head"><div><span>本轮任务</span><small>${round.total || 0} 条记录 · 点击任务行查看完整内容</small></div><button class="patrol-round-delete" type="button" data-delete-round="${escapeHtml(round.id)}"><i class="fas fa-trash"></i> 删除整轮记录</button></div>${tasks || '<div class="patrol-empty">本轮没有任务</div>'}</div>
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
        text_test_count: Number(document.getElementById('patrol-text-count').value),
        image_test_count: Number(document.getElementById('patrol-image-count').value),
        image_min_count: Number(document.getElementById('patrol-image-min').value),
        image_max_count: Number(document.getElementById('patrol-image-max').value),
        models: [...document.querySelectorAll('#patrol-models input:checked')].map(input => input.value),
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

function fileToBase64(file) {
    return new Promise((resolve, reject) => {
        const reader = new FileReader();
        reader.onload = () => resolve(String(reader.result).split(',', 2)[1]);
        reader.onerror = () => reject(reader.error);
        reader.readAsDataURL(file);
    });
}

async function uploadImages(files) {
    const allowed = new Set(['image/png', 'image/jpeg', 'image/gif', 'image/webp']);
    const selected = [...files];
    if (!selected.length) return;
    const button = document.getElementById('patrol-upload-btn');
    button.disabled = true;
    try {
        for (let index = 0; index < selected.length; index++) {
            const file = selected[index];
            if (!allowed.has(file.type) || file.size > 10 * 1024 * 1024) {
                throw new Error(`${file.name} 不是受支持的图片或超过 10 MB`);
            }
            button.innerHTML = `<i class="fas fa-spinner fa-spin"></i> ${index + 1}/${selected.length}`;
            await apiCall('POST', '/admin/patrol/images', { name: file.name, data_base64: await fileToBase64(file) });
        }
        showToast(`已上传 ${selected.length} 张测试图片`, 'success');
        await loadPatrol();
    } catch (error) {
        showToast(`上传失败：${error.message}`, 'error');
    } finally {
        button.disabled = false;
        button.innerHTML = '<i class="fas fa-cloud-arrow-up"></i> 上传图片';
    }
}

async function deleteImage(imageId) {
    if (!window.confirm('确定从盘巡素材库删除这张图片吗？')) return;
    try {
        await apiCall('DELETE', `/admin/patrol/images/${encodeURIComponent(imageId)}`);
        if (thumbnailUrls.has(imageId)) URL.revokeObjectURL(thumbnailUrls.get(imageId));
        thumbnailUrls.delete(imageId);
        showToast('图片已删除', 'success');
        await loadPatrol();
    } catch (error) {
        showToast(`删除失败：${error.message}`, 'error');
    }
}

async function deleteRound(roundId) {
    if (!window.confirm('确定删除这一整轮盘巡记录吗？本轮内的所有文字和图文任务都会删除，且无法恢复。')) return;
    try {
        await apiCall('DELETE', `/admin/patrol/rounds/${encodeURIComponent(roundId)}`);
        selectedRoundIds.delete(roundId);
        showToast('整轮盘巡记录已删除', 'success');
        await loadPatrol();
    } catch (error) {
        showToast(`删除轮次失败：${error.message}`, 'error');
    }
}

function updateRoundSelectionControls() {
    const checkboxes = [...document.querySelectorAll('[data-select-round]')];
    const selectAll = document.getElementById('patrol-select-all');
    const deleteButton = document.getElementById('patrol-delete-selected');
    const selectedCount = document.getElementById('patrol-selected-count');
    if (selectAll) {
        selectAll.disabled = checkboxes.length === 0;
        selectAll.checked = checkboxes.length > 0 && selectedRoundIds.size === checkboxes.length;
        selectAll.indeterminate = selectedRoundIds.size > 0 && selectedRoundIds.size < checkboxes.length;
    }
    if (deleteButton) deleteButton.disabled = selectedRoundIds.size === 0;
    if (selectedCount) selectedCount.textContent = selectedRoundIds.size;
    checkboxes.forEach(checkbox => checkbox.closest('.patrol-round')?.classList.toggle('is-selected', checkbox.checked));
}

async function deleteSelectedRounds() {
    const roundIds = [...selectedRoundIds];
    if (!roundIds.length || !window.confirm(`确定删除选中的 ${roundIds.length} 轮盘巡记录吗？其中所有文字和图文任务都会删除，且无法恢复。`)) return;
    const button = document.getElementById('patrol-delete-selected');
    button.disabled = true;
    try {
        const result = await apiCall('POST', '/admin/patrol/rounds/delete', { round_ids: roundIds });
        selectedRoundIds.clear();
        showToast(`已删除 ${result.deleted_count || 0} 轮盘巡记录`, 'success');
        await loadPatrol();
    } catch (error) {
        showToast(`批量删除失败：${error.message}`, 'error');
        updateRoundSelectionControls();
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
    document.getElementById('patrol-delete-selected')?.addEventListener('click', deleteSelectedRounds);
    document.getElementById('patrol-select-all')?.addEventListener('change', event => {
        document.querySelectorAll('[data-select-round]').forEach(checkbox => {
            checkbox.checked = event.target.checked;
            if (checkbox.checked) selectedRoundIds.add(checkbox.dataset.selectRound);
            else selectedRoundIds.delete(checkbox.dataset.selectRound);
        });
        updateRoundSelectionControls();
    });
    const imageInput = document.getElementById('patrol-image-input');
    document.getElementById('patrol-upload-btn')?.addEventListener('click', () => imageInput?.click());
    imageInput?.addEventListener('change', event => {
        uploadImages(event.target.files);
        event.target.value = '';
    });
    document.getElementById('patrol-image-library')?.addEventListener('click', event => {
        const button = event.target.closest('[data-delete-image]');
        if (button) deleteImage(button.dataset.deleteImage);
    });
    document.getElementById('patrol-history')?.addEventListener('click', event => {
        if (event.target.closest('[data-select-round]')) {
            event.stopPropagation();
            return;
        }
        const button = event.target.closest('[data-delete-round]');
        if (button) deleteRound(button.dataset.deleteRound);
    });
    document.getElementById('patrol-history')?.addEventListener('change', event => {
        const checkbox = event.target.closest('[data-select-round]');
        if (!checkbox) return;
        if (checkbox.checked) selectedRoundIds.add(checkbox.dataset.selectRound);
        else selectedRoundIds.delete(checkbox.dataset.selectRound);
        updateRoundSelectionControls();
    });
    clearInterval(timer);
    timer = setInterval(() => {
        if (isRunning && document.getElementById('patrol')?.classList.contains('active')) loadPatrol();
    }, 3000);
}

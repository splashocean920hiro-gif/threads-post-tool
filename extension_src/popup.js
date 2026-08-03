function $(id) { return document.getElementById(id); }

async function loadSettings() {
  const settings = await chrome.runtime.sendMessage({ cmd: 'getSettings' });
  $('archiveUrl').value = settings.archiveUrl || '';
  $('ingestKey').value = settings.ingestKey || '';
  $('scoreThreshold').value = settings.scoreThreshold || 500;
  $('keywords').value = (settings.keywords || []).join('\n');
  $('debugMode').checked = !!settings.debugMode;
}

function parseKeywords() {
  return $('keywords').value.split(/[\n\r]+/).map((s) => s.trim().replace(/^#/, '')).filter(Boolean);
}

function labelForStatus(s) {
  return {
    idle: '待機中', running: '実行中', paused: '一時停止（要確認）', completed: '✅ リサーチ完了',
    session_cooldown: '⏳ セッション上限（自動待機中）', daily_limit: '🌙 本日の上限に到達',
  }[s] || s;
}

function fmtRemaining(ms) {
  if (ms <= 0) return 'まもなく';
  const totalMin = Math.ceil(ms / 60000);
  const h = Math.floor(totalMin / 60);
  const m = totalMin % 60;
  return h > 0 ? `${h}時間${m}分後` : `${m}分後`;
}

async function refreshStatus() {
  const state = await chrome.runtime.sendMessage({ cmd: 'getStatus' });
  const settings = await chrome.runtime.sendMessage({ cmd: 'getSettings' });
  const keywords = settings.keywords || [];
  const currentKeyword = keywords.length ? keywords[(state.keywordIndex || 0) % keywords.length] : '';
  const detail = `対象: ${currentKeyword ? '#' + currentKeyword : '未設定'} ・ 候補キュー: ${state.candidates ? state.candidates.length : 0}件 ・ 処理済み: ${state.processed ? state.processed.length : 0}件`;

  let capNote = '';
  if (state.status === 'session_cooldown' && state.cooldownUntil) {
    capNote = `<br>⏳ 次のセッションまで: ${fmtRemaining(state.cooldownUntil - Date.now())}`;
  } else if (state.status === 'daily_limit') {
    capNote = `<br>🌙 本日はここまでです。日付が変わったら「🔍 バズ投稿発見を開始」で再開してください。`;
  }

  $('status').innerHTML = `
    状態: <b>${labelForStatus(state.status)}</b><br>
    ${detail}<br>
    セッション: ${state.sessionCount || 0}件 ・ 本日: ${state.dailyCount || 0}件${capNote}
  `;
  $('pausedActions').style.display = state.status === 'paused' ? 'flex' : 'none';
  $('completedBanner').style.display = state.status === 'completed' ? 'block' : 'none';

  const log = (state.log || []).slice(-8).reverse();
  $('log').innerHTML = log.map((e) => {
    const mark = e.ok ? '✅' : '⚠️';
    const detailText = e.ok
      ? `👍${e.likes ?? '-'} 💬${e.replies ?? '-'} 👁${e.impressions ?? '-'}`
      : (e.reason || e.error || '');
    return `<div>${mark} ${e.key || ''}: ${detailText}</div>`;
  }).join('');
}

$('saveBtn').onclick = async () => {
  await chrome.runtime.sendMessage({
    cmd: 'saveSettings',
    settings: {
      archiveUrl: $('archiveUrl').value.trim().replace(/\/$/, ''),
      ingestKey: $('ingestKey').value.trim(),
      scoreThreshold: parseInt($('scoreThreshold').value, 10) || 500,
      keywords: parseKeywords(),
      debugMode: $('debugMode').checked,
    },
  });
  $('saveBtn').textContent = '💾 保存しました';
  setTimeout(() => { $('saveBtn').textContent = '💾 設定を保存'; }, 1500);
};

$('startDiscover').onclick = async () => {
  const threshold = parseInt($('scoreThreshold').value, 10) || 500;
  const keywords = parseKeywords();
  await chrome.runtime.sendMessage({ cmd: 'saveSettings', settings: { scoreThreshold: threshold, keywords } });
  const res = await chrome.runtime.sendMessage({ cmd: 'startDiscover', threshold });
  if (!res.ok) alert(res.error);
  refreshStatus();
};

$('stopBtn').onclick = async () => {
  await chrome.runtime.sendMessage({ cmd: 'stop' });
  refreshStatus();
};
$('resetProcessedBtn').onclick = async () => {
  if (!confirm('処理済みリストをリセットします。アーカイブ側を削除済みの場合など、同じ投稿を再度発見・収集できるようになります。よろしいですか？')) return;
  await chrome.runtime.sendMessage({ cmd: 'resetProcessed' });
  refreshStatus();
};
$('resumeBtn').onclick = async () => {
  await chrome.runtime.sendMessage({ cmd: 'resume' });
  refreshStatus();
};
$('resumeAlwaysBtn').onclick = async () => {
  await chrome.runtime.sendMessage({ cmd: 'resume' });
  refreshStatus();
};
$('skipBtn').onclick = async () => {
  await chrome.runtime.sendMessage({ cmd: 'skip' });
  refreshStatus();
};

$('settingsToggle').onclick = () => {
  const panel = $('settingsPanel');
  panel.style.display = panel.style.display === 'none' ? 'block' : 'none';
};

loadSettings();
refreshStatus();
setInterval(refreshStatus, 1500);

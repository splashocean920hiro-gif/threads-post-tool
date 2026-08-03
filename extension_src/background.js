// config.js は配布zipにのみ入る（サーバがユーザーごとに生成する）。
// 自分でソースから読み込む開発時は存在しないので、失敗しても落とさない。
try { importScripts('config.js'); } catch (e) { /* 開発時は config.js なしが正常 */ }

const DEFAULTS = {
  archiveUrl: (self.PRESET && self.PRESET.archiveUrl) || '',
  ingestKey: (self.PRESET && self.PRESET.ingestKey) || '',
  sessionCap: 25,
  dailyCap: 80,
  sessionCooldownMinutes: 120,
  scoreThreshold: 500, // いいね+リプライの合計（表示回数は他人の投稿では非公開のため使わない）
  keywords: [],
  minDelayMs: 8000,
  maxDelayMs: 20000,
  longPauseChance: 0.15,
  longPauseMinMs: 30000,
  longPauseMaxMs: 60000,
  debugMode: false, // ONにするとThreads投稿ページを開くだけでDOM検出の詳細をコンソール出力
};

const EMPTY_STATE = {
  status: 'idle', // idle | running | paused | completed | session_cooldown | daily_limit
  tabId: null,
  sessionCount: 0,
  dailyCount: 0,
  dailyDate: null,
  cooldownUntil: null,
  log: [],

  candidates: [], // {postKey, url, likes, replies, impressions}
  current: null,
  processed: [], // 直近500件のpostKey（重複防止）
  keywordIndex: 0,
  lapNewCount: 0,
};

async function getSettings() {
  const stored = (await chrome.storage.local.get(['settings'])).settings || {};
  // 配布zipのトークンが変わったら(=再発行して入れ直したら)、保存済み設定を更新する。
  // saveSettings が {...現在値, ...変更分} で保存するため、一度でも設定を保存すると
  // PRESET 由来のキーが storage に焼き込まれる。未パック拡張のIDはフォルダのパス由来で
  // 入れ直しても storage が残るので、これが無いと再発行後ずっと401のままになる。
  if (self.PRESET && self.PRESET.presetId && stored.presetId !== self.PRESET.presetId) {
    const merged = {
      ...stored,
      ingestKey: self.PRESET.ingestKey,
      archiveUrl: self.PRESET.archiveUrl,
      presetId: self.PRESET.presetId,
    };
    await chrome.storage.local.set({ settings: merged });
    return { ...DEFAULTS, ...merged };
  }
  return { ...DEFAULTS, ...stored };
}

async function saveSettings(patch) {
  const current = await getSettings();
  await chrome.storage.local.set({ settings: { ...current, ...patch } });
}

async function getState() {
  const stored = await chrome.storage.local.get(['state']);
  return { ...EMPTY_STATE, ...(stored.state || {}) };
}

async function setState(patch) {
  const state = await getState();
  const next = { ...state, ...patch };
  await chrome.storage.local.set({ state: next });
  return next;
}

function todayStr() { return new Date().toISOString().slice(0, 10); }
function randomDelay(min, max) { return Math.floor(min + Math.random() * (max - min)); }

async function resetDailyIfNeeded() {
  const state = await getState();
  if (state.dailyDate !== todayStr()) {
    await setState({ dailyDate: todayStr(), dailyCount: 0 });
  }
}

async function fetchArchive(settings, path, method, body) {
  const res = await fetch(`${settings.archiveUrl}${path}`, {
    method,
    headers: { 'Content-Type': 'application/json', 'X-Ingest-Key': settings.ingestKey },
    body: body ? JSON.stringify(body) : undefined,
  });
  if (!res.ok) {
    const text = await res.text().catch(() => '');
    throw new Error(`${path} 失敗: HTTP ${res.status} ${text}`.trim());
  }
  return res.json().catch(() => ({}));
}

function pushLog(state, entry) {
  return [...(state.log || []), entry].slice(-100);
}

// ---------- タブ操作 ----------
async function navigateTab(url) {
  const state = await getState();
  if (state.tabId) {
    try {
      await chrome.tabs.update(state.tabId, { url });
      return;
    } catch (e) { /* タブが閉じられていた等 → 新規タブへ */ }
  }
  const [active] = await chrome.tabs.query({ active: true, currentWindow: true });
  if (active) {
    await chrome.tabs.update(active.id, { url });
    await setState({ tabId: active.id });
  } else {
    const created = await chrome.tabs.create({ url });
    await setState({ tabId: created.id });
  }
}

function isSearchPath(path) {
  return path === '/search' || path === '/search/';
}

function notifyResearchComplete(keywords) {
  try {
    chrome.notifications.create({
      type: 'basic',
      iconUrl: chrome.runtime.getURL('icon.png'),
      title: '🔥 Threadsバズ投稿発見 完了',
      message: `設定したキーワード（${keywords.join('、')}）でリサーチした全リストのアーカイブが完了しました。キーワードを変更してください。`,
      priority: 2,
    });
  } catch (e) { /* 通知が使えない環境でも本体機能には影響させない */ }
}

function keywordToUrl(keyword) {
  return `https://www.threads.com/search?q=${encodeURIComponent(keyword.trim())}&serp_type=default`;
}

async function navigateToDiscoveryHome(advanceKeyword) {
  const settings = await getSettings();
  const keywords = (settings.keywords || []).filter(Boolean);
  if (keywords.length === 0) {
    await setState({ status: 'paused' });
    return;
  }
  const state = await getState();
  let idx = state.keywordIndex || 0;
  let lapNewCount = state.lapNewCount || 0;
  if (advanceKeyword) {
    idx = (idx + 1) % keywords.length;
    if (idx === 0) {
      if (lapNewCount === 0) {
        await setState({ status: 'completed', keywordIndex: 0, lapNewCount: 0 });
        notifyResearchComplete(keywords);
        return;
      }
      lapNewCount = 0;
    }
  }
  await setState({ keywordIndex: idx, lapNewCount });
  const keyword = keywords[idx % keywords.length];
  await navigateTab(keywordToUrl(keyword));
}

chrome.tabs.onRemoved.addListener(async (tabId) => {
  const state = await getState();
  if (state.tabId === tabId && state.status === 'running') {
    await setState({ status: 'paused', tabId: null });
  }
});

// ---------- discover ----------
async function startDiscover(threshold) {
  await resetDailyIfNeeded();
  if (threshold) await saveSettings({ scoreThreshold: threshold });
  chrome.alarms.clear('buzz-session-cooldown');
  await setState({
    status: 'running', candidates: [], current: null, sessionCount: 0, tabId: null, log: [],
    keywordIndex: 0, lapNewCount: 0, cooldownUntil: null,
  });
  await navigateToDiscoveryHome(false);
}

async function advanceDiscover() {
  const state = await getState();
  if (state.candidates.length > 0) {
    const [next, ...rest] = state.candidates;
    await setState({ current: next, candidates: rest });
    await navigateTab(next.url);
  } else {
    await navigateToDiscoveryHome(true);
  }
}

chrome.alarms.onAlarm.addListener(async (alarm) => {
  if (alarm.name === 'buzz-session-cooldown') {
    const state = await getState();
    if (state.status !== 'session_cooldown') return;
    await setState({ status: 'running', sessionCount: 0, cooldownUntil: null });
    await advanceDiscover();
    return;
  }
  const state = await getState();
  if (state.status !== 'running') return;
  if (alarm.name === 'buzz-discover-next') {
    await advanceDiscover();
  }
});

function scheduleNext(alarmName, settings) {
  const useLongPause = Math.random() < settings.longPauseChance;
  const delay = useLongPause
    ? randomDelay(settings.longPauseMinMs, settings.longPauseMaxMs)
    : randomDelay(settings.minDelayMs, settings.maxDelayMs);
  chrome.alarms.create(alarmName, { when: Date.now() + delay });
}

async function handleCapsOrScheduleNext(settings, dailyCount, sessionCount) {
  if (dailyCount >= settings.dailyCap) {
    await setState({ status: 'daily_limit', cooldownUntil: null });
  } else if (sessionCount >= settings.sessionCap) {
    const cooldownUntil = Date.now() + settings.sessionCooldownMinutes * 60 * 1000;
    await setState({ status: 'session_cooldown', cooldownUntil });
    chrome.alarms.create('buzz-session-cooldown', { when: cooldownUntil });
  } else {
    scheduleNext('buzz-discover-next', settings);
  }
}

// ---------- メッセージハンドラ ----------
chrome.runtime.onMessage.addListener((msg, _sender, sendResponse) => {
  (async () => {
    switch (msg.cmd) {
      case 'getStatus':
        sendResponse(await getState());
        break;

      case 'getSettings':
        sendResponse(await getSettings());
        break;

      case 'saveSettings':
        await saveSettings(msg.settings);
        sendResponse({ ok: true });
        break;

      case 'startDiscover':
        try {
          const settings = await getSettings();
          if ((settings.keywords || []).filter(Boolean).length === 0) {
            throw new Error('キーワード/ハッシュタグを最低1つ設定してください');
          }
          await startDiscover(msg.threshold);
          sendResponse({ ok: true });
        } catch (e) {
          sendResponse({ ok: false, error: String(e.message || e) });
        }
        break;

      case 'stop':
        chrome.alarms.clear('buzz-discover-next');
        chrome.alarms.clear('buzz-session-cooldown');
        await setState({ status: 'idle', cooldownUntil: null });
        sendResponse({ ok: true });
        break;

      case 'resetProcessed':
        await setState({ processed: [] });
        sendResponse({ ok: true });
        break;

      case 'resume': {
        const prev = await getState();
        const patch = { status: 'running' };
        if (prev.status === 'session_cooldown') {
          patch.sessionCount = 0;
          patch.cooldownUntil = null;
          chrome.alarms.clear('buzz-session-cooldown');
        }
        const state = await setState(patch);
        if (state.current) await navigateTab(state.current.url);
        else await advanceDiscover();
        sendResponse({ ok: true });
        break;
      }

      case 'skip': {
        await setState({ status: 'running', current: null });
        await advanceDiscover();
        sendResponse({ ok: true });
        break;
      }

      case 'checkActive': {
        const state = await getState();
        if (state.status !== 'running') { sendResponse({ active: false }); break; }

        const path = msg.path || '';
        if (isSearchPath(path)) {
          const settings = await getSettings();
          sendResponse({
            active: true, phase: 'search',
            threshold: settings.scoreThreshold, alreadyProcessed: state.processed,
          });
        } else if (state.current) {
          sendResponse({ active: true, phase: 'post', current: state.current });
        } else {
          sendResponse({ active: false });
        }
        break;
      }

      case 'searchCandidates': {
        const state = await getState();
        const seen = new Set([
          ...(state.processed || []),
          ...(state.candidates || []).map((c) => c.postKey),
          state.current ? state.current.postKey : null,
        ].filter(Boolean));
        const fresh = (msg.candidates || []).filter((c) => !seen.has(c.postKey));
        const candidates = [...(state.candidates || []), ...fresh];
        const lapNewCount = (state.lapNewCount || 0) + fresh.length;
        await setState({ candidates, lapNewCount });
        const latest = await getState();
        if (!latest.current && latest.status === 'running') {
          await advanceDiscover();
        }
        sendResponse({ ok: true });
        break;
      }

      case 'postResult': {
        const settings = await getSettings();
        const state = await getState();
        const current = state.current;
        try {
          if (!current || current.postKey !== msg.postKey) throw new Error('状態が一致しません（キューが進んでいる可能性）');
          if (!settings.archiveUrl) throw new Error('アーカイブURLが未設定です。ツールの「初期設定・拡張機能」からダウンロードし直してください');
          if (!settings.ingestKey) throw new Error('Ingestキーが未設定です');

          await fetchArchive(settings, '/ingest', 'POST', {
            url: current.url,
            text: msg.text || '',
            posted_at: msg.postedAt || '',
            likes: Number.isInteger(msg.likes) ? msg.likes : null,
            reply_count: Number.isInteger(msg.replies) ? msg.replies : null,
            impressions: Number.isInteger(msg.impressions) ? msg.impressions : null,
            replies: Array.isArray(msg.replyTexts) ? msg.replyTexts : [], // 本人の連投(ツリー)本文
            keyword: (settings.keywords || [])[state.keywordIndex] || '',
          });

          const processed = [...(state.processed || []), current.postKey].slice(-500);
          const sessionCount = state.sessionCount + 1;
          const dailyCount = state.dailyCount + 1;
          const log = pushLog(state, {
            time: Date.now(), key: current.postKey, ok: true,
            likes: msg.likes, replies: msg.replies, impressions: msg.impressions,
          });
          const updated = await setState({ processed, sessionCount, dailyCount, current: null, log });

          if (updated.status === 'running') {
            await handleCapsOrScheduleNext(settings, dailyCount, sessionCount);
          }
          sendResponse({ ok: true });
        } catch (e) {
          const log = pushLog(state, { time: Date.now(), key: msg.postKey, error: String(e.message || e), ok: false });
          await setState({ status: 'paused', log });
          sendResponse({ ok: false, error: String(e.message || e) });
        }
        break;
      }

      case 'postAnomaly': {
        const state = await getState();
        const log = pushLog(state, { time: Date.now(), key: msg.postKey, reason: msg.reason, ok: false });
        await setState({ status: 'paused', log });
        sendResponse({ ok: true });
        break;
      }

      default:
        sendResponse({ ok: false, error: 'unknown cmd' });
    }
  })();
  return true;
});

// config.js は配布zipにのみ入る（サーバがユーザーごとに生成する）。
// 自分でソースから読み込む開発時は存在しないので、失敗しても落とさない。
try { importScripts('config.js'); } catch (e) { /* 開発時は config.js なしが正常 */ }

// ---------------------------------------------------------------------------
// 2モード構成
//   discover : キーワード検索を見て回り、アカウント単位で集計する。**保存はしない**
//   archive  : アカウントURLを指定し、その投稿を開いて表示回数の高いものだけ保存する
//
// 待機（人間的な揺らぎ）は必ずコンテンツスクリプト側で行う。
// chrome.alarms は MV3 で30秒未満が丸められるため、ペース制御には使えない
// （旧実装の minDelayMs 8秒〜20秒は実際には効いていなかった）。
//
// 実行の可否は report.html（コントローラ）のハートビートで決まる。
// タブを閉じるとハートビートが止まり、コンテンツ側が次のステップに進まなくなる。
// ---------------------------------------------------------------------------

const DEFAULTS = {
  archiveUrl: (self.PRESET && self.PRESET.archiveUrl) || '',
  ingestKey: (self.PRESET && self.PRESET.ingestKey) || '',
  // 人間的な揺らぎ
  minDelayMs: 4000,
  maxDelayMs: 11000,
  longPauseChance: 0.1,
  longPauseMinMs: 20000,
  longPauseMaxMs: 40000,
  // 上限
  discoverScrollMax: 20,   // モード1: 検索結果のスクロール回数
  archivePostMax: 30,      // モード2: 開く詳細ページの数
  archiveSendTop: 10,      // モード2: 送信する上位件数
  followerFillMax: 5,      // モード1: フォロワー数を補完するアカウント数
  cooldownMinutes: 10,     // 実行終了から次を始められるまで
  reportAccountMax: 200,   // レポートに保持するアカウント数
  reportSampleMax: 30,     // 1アカウントあたりの生データ保持件数
  debugMode: false,
};

const EMPTY_STATE = {
  schema: 2,
  mode: null,            // 'discover' | 'archive'
  status: 'idle',        // idle | running | done | stopped | stopped_error
  statusText: '',
  tabId: null,
  log: [],
  cooldownUntil: null,
  errorUntil: null,
  // discover
  keyword: null,
  // archive
  profileUrl: null,
  accountId: null,
  handle: null,
  followers: null,
  queue: [],             // [{postKey, url}]
  buffer: [],            // 収集済み（送信前）
  bufferAt: null,        // バッファの作成時刻（24時間で破棄）
  current: null,
};

async function getSettings() {
  const stored = (await chrome.storage.local.get(['settings'])).settings || {};
  const merged = { ...DEFAULTS, ...stored };
  if (self.PRESET) {
    if (self.PRESET.archiveUrl) merged.archiveUrl = self.PRESET.archiveUrl;
    if (self.PRESET.ingestKey) merged.ingestKey = self.PRESET.ingestKey;
  }
  return merged;
}

async function saveSettings(patch) {
  const cur = (await chrome.storage.local.get(['settings'])).settings || {};
  await chrome.storage.local.set({ settings: { ...cur, ...patch } });
}

async function getState() {
  const s = (await chrome.storage.local.get(['state'])).state;
  if (!s || s.schema !== EMPTY_STATE.schema) return { ...EMPTY_STATE };
  return { ...EMPTY_STATE, ...s };
}

async function setState(patch) {
  const cur = await getState();
  const next = { ...cur, ...patch };
  await chrome.storage.local.set({ state: next });
  return next;
}

function nowLog(state, entry) {
  const t = new Date().toLocaleTimeString('ja-JP');
  return [...(state.log || []), `${t} ${entry}`].slice(-100);
}

async function log(entry, extra = {}) {
  const s = await getState();
  await setState({ log: nowLog(s, entry), ...extra });
}

// ---------- サーバ通信（モード2でのみ使う） ----------
async function fetchArchive(settings, path, method, body) {
  if (!settings.archiveUrl || !settings.ingestKey) {
    throw new Error('保存先が設定されていません');
  }
  const res = await fetch(`${settings.archiveUrl}${path}`, {
    method,
    headers: { 'Content-Type': 'application/json', 'X-Ingest-Key': settings.ingestKey },
    body: body ? JSON.stringify(body) : undefined,
  });
  if (!res.ok) throw new Error(`サーバが ${res.status} を返しました`);
  return res.json().catch(() => ({}));
}

// ---------- タブ操作 ----------
// 収集した候補URLは Threads のDOM由来なので、遷移前にここでも必ず検証する。
// content.js 側でも弾いているが、片方が破られても外部サイトへ飛ばないようにする。
const THREADS_ORIGINS = ['https://www.threads.com', 'https://www.threads.net'];
function isThreadsUrl(u) {
  try { return THREADS_ORIGINS.includes(new URL(u).origin); } catch (e) { return false; }
}

async function navigateTab(url) {
  if (!isThreadsUrl(url)) {
    await stopRun(`Threads以外のURLへ遷移しようとしたため停止しました`, true);
    return;
  }
  const state = await getState();
  // 既に使っている作業タブがあれば、それを使い回す
  if (state.tabId) {
    try {
      await chrome.tabs.update(state.tabId, { url });
      return;
    } catch (e) {
      await log(`作業タブが見つからないので開き直します（${e.message}）`);
    }
  }
  const reportUrl = chrome.runtime.getURL('report.html');
  // 既に開いている Threads のタブを探す（レポート画面は絶対に潰さない）。
  // currentWindow に限定すると別ウィンドウのタブを見落とすので、全ウィンドウから探す。
  let target = null;
  try {
    const tabs = await chrome.tabs.query({});
    target = tabs.find((t) => t.url && /^https:\/\/www\.threads\.(com|net)\//.test(t.url)
                              && !t.url.startsWith(reportUrl));
  } catch (e) {
    await log(`タブ一覧を取得できませんでした: ${e.message}`);
  }
  if (target) {
    try {
      await chrome.tabs.update(target.id, { url, active: true });
      await setState({ tabId: target.id });
      await log(`既存のThreadsタブを使います`);
      return;
    } catch (e) {
      await log(`既存タブを使えませんでした: ${e.message}`);
    }
  }
  try {
    // active:true にする。裏で開くと「開かれない」と誤解されるうえ、
    // Chromeが裏タブのスクリプトを間引くことがある
    const created = await chrome.tabs.create({ url, active: true });
    await setState({ tabId: created.id });
    await log('新しいタブでThreadsを開きました');
  } catch (e) {
    await stopRun(`タブを開けませんでした: ${e.message}`, true);
  }
}


async function openController() {
  const url = chrome.runtime.getURL('report.html');
  const tabs = await chrome.tabs.query({ url });
  if (tabs.length) { await chrome.tabs.update(tabs[0].id, { active: true }); return; }
  await chrome.tabs.create({ url });
}

// ---------- 開始・停止 ----------
async function canStart() {
  const s = await getState();
  const now = Date.now();
  if (s.errorUntil && now < s.errorUntil) {
    return `エラー検知のため停止中です（あと${Math.ceil((s.errorUntil - now) / 60000)}分）`;
  }
  if (s.cooldownUntil && now < s.cooldownUntil) {
    return `連続実行を避けるため待機中です（あと${Math.ceil((s.cooldownUntil - now) / 60000)}分）`;
  }
  return null;
}

async function startDiscover(keyword) {
  const blocked = await canStart();
  if (blocked) { await setState({ statusText: blocked }); return; }
  await setState({
    ...EMPTY_STATE, mode: 'discover', status: 'running', keyword,
    statusText: `「${keyword}」を調べています`, log: [],
  });
  await log(`発見モード開始: ${keyword}`);
  await navigateTab(`https://www.threads.com/search?q=${encodeURIComponent(keyword)}&serp_type=default`);
}

async function startArchive(profileUrl, accountId) {
  const blocked = await canStart();
  if (blocked) { await setState({ statusText: blocked }); return; }
  const handle = (profileUrl.match(/\/@([^/?#]+)/) || [])[1] || null;
  const prev = await getState();
  // 24時間以内の同じアカウントのバッファがあれば引き継ぐ（中断からの再開）
  const fresh = prev.bufferAt && (Date.now() - prev.bufferAt < 24 * 3600 * 1000)
                && prev.handle === handle;
  await setState({
    ...EMPTY_STATE, mode: 'archive', status: 'running',
    profileUrl, accountId, handle,
    buffer: fresh ? (prev.buffer || []) : [],
    bufferAt: fresh ? prev.bufferAt : Date.now(),
    statusText: `@${handle} の投稿を集めています`, log: [],
  });
  await log(`アーカイブ開始: @${handle}${fresh ? `（前回の${(prev.buffer || []).length}件を引き継ぎ）` : ''}`);
  await navigateTab(profileUrl);
}

async function stopRun(reason, isError) {
  const s = await getState();
  const patch = {
    status: isError ? 'stopped_error' : 'stopped',
    statusText: reason,
    current: null,
    cooldownUntil: Date.now() + DEFAULTS.cooldownMinutes * 60 * 1000,
  };
  if (isError) patch.errorUntil = Date.now() + 30 * 60 * 1000;
  await setState({ ...patch, log: nowLog(s, reason) });
}

// ---------- モード2: 収集の前進と送信 ----------
async function advanceArchive() {
  const s = await getState();
  if (s.status !== 'running') return;
  const settings = await getSettings();
  if (s.queue.length === 0 || (s.buffer || []).length >= settings.archivePostMax) {
    await finishArchive();
    return;
  }
  const [next, ...rest] = s.queue;
  await setState({ current: next, queue: rest,
    statusText: `@${s.handle} の投稿を確認中（${(s.buffer || []).length + 1}/${settings.archivePostMax}）` });
  await navigateTab(next.url);
}

async function finishArchive() {
  const s = await getState();
  const settings = await getSettings();
  const buf = (s.buffer || []).filter((b) => b && b.url);
  if (!buf.length) { await stopRun('保存できる投稿がありませんでした', false); return; }
  // 全部見てから上位を決める（都度送信だと上位が決まらない）
  const top = [...buf].sort((a, b) => (b.impressions || 0) - (a.impressions || 0))
                      .slice(0, settings.archiveSendTop);
  let ok = 0;
  for (const p of top) {
    try {
      await fetchArchive(settings, '/ingest', 'POST', {
        url: p.url, text: p.text || '', posted_at: p.postedAt || '',
        likes: p.likes, reply_count: p.replies, impressions: p.impressions,
        replies: p.replyTexts || [],
        author: s.handle || '', author_followers: s.followers,
        account_id: s.accountId || '',
      });
      ok += 1;
    } catch (e) {
      await log(`保存に失敗: ${e.message}`);
    }
  }
  await setState({ buffer: [], bufferAt: null });
  await stopRun(`@${s.handle} から ${ok}件を保存しました（${buf.length}件中の上位）`, false);
}

// ---------- モード1: レポートへの集計 ----------
async function mergeReport(handle, samples) {
  const settings = await getSettings();
  const data = await chrome.storage.local.get(['report']);
  const report = data.report || {};
  const cur = report[handle] || { handle, samples: [], followers: null, seenAt: 0 };
  const known = new Set(cur.samples.map((x) => x.postKey));
  for (const s of samples) {
    if (!s || known.has(s.postKey)) continue;
    cur.samples.push(s);
    known.add(s.postKey);
  }
  cur.samples = cur.samples.slice(-settings.reportSampleMax);
  cur.seenAt = Date.now();
  report[handle] = cur;
  // 件数上限。古い順に落とす
  const keys = Object.keys(report);
  if (keys.length > settings.reportAccountMax) {
    keys.sort((a, b) => (report[a].seenAt || 0) - (report[b].seenAt || 0));
    for (const k of keys.slice(0, keys.length - settings.reportAccountMax)) delete report[k];
  }
  await chrome.storage.local.set({ report });
}

// ---------- メッセージ ----------
chrome.runtime.onMessage.addListener((msg, _sender, sendResponse) => {
  (async () => {
    switch (msg.cmd) {
      case 'getStatus': {
        const s = await getState();
        sendResponse({ mode: s.mode, status: s.status, statusText: s.statusText, log: s.log });
        break;
      }
      case 'getSettings': sendResponse(await getSettings()); break;
      case 'saveSettings': await saveSettings(msg.patch || {}); sendResponse({ ok: true }); break;
      case 'openController': await openController(); sendResponse({ ok: true }); break;

      case 'listAccounts': {
        try {
          const settings = await getSettings();
          const r = await fetchArchive(settings, '/ingest/accounts', 'GET');
          sendResponse({ ok: true, accounts: r.accounts || [] });
        } catch (e) { sendResponse({ ok: false, error: e.message }); }
        break;
      }

      case 'startDiscover': await startDiscover(msg.keyword); sendResponse({ ok: true }); break;
      case 'startArchive': await startArchive(msg.profileUrl, msg.accountId); sendResponse({ ok: true }); break;
      case 'stop': await stopRun('停止しました', false); sendResponse({ ok: true }); break;

      // コンテンツスクリプトが「今このページで何をすべきか」を尋ねる
      case 'checkActive': {
        const s = await getState();
        if (s.status !== 'running') { sendResponse({ active: false }); break; }
        const settings = await getSettings();
        const path = msg.path || '';
        if (s.mode === 'discover' && (path === '/search' || path === '/search/')) {
          sendResponse({ active: true, phase: 'discover', pace: pace(settings),
                         scrollMax: settings.discoverScrollMax });
        } else if (s.mode === 'archive' && s.current
                   && path.includes(`/post/${s.current.postKey}`)) {
          sendResponse({ active: true, phase: 'post', current: s.current, pace: pace(settings) });
        } else if (s.mode === 'archive' && !s.current && /^\/@[^/]+\/?$/.test(path)) {
          sendResponse({ active: true, phase: 'profile', pace: pace(settings),
                         postMax: settings.archivePostMax });
        } else {
          sendResponse({ active: false });
        }
        break;
      }

      // モード1: 集計結果（サーバへは送らない）
      case 'discoverSamples': {
        for (const [handle, samples] of Object.entries(msg.byHandle || {})) {
          await mergeReport(handle, samples);
        }
        sendResponse({ ok: true });
        break;
      }
      case 'discoverDone': {
        await stopRun(`「${(await getState()).keyword}」の調査が終わりました`, false);
        sendResponse({ ok: true });
        break;
      }

      // モード2: プロフィールから投稿一覧とフォロワー数
      case 'profileResult': {
        const s = await getState();
        if (s.status !== 'running' || s.mode !== 'archive') { sendResponse({ ok: false }); break; }
        const settings = await getSettings();
        const queue = (msg.candidates || []).slice(0, settings.archivePostMax);
        await setState({ followers: msg.followers ?? null, queue });
        await log(`@${s.handle} の投稿 ${queue.length}件を確認します（フォロワー ${msg.followers ?? '不明'}）`);
        await advanceArchive();
        sendResponse({ ok: true });
        break;
      }

      // モード2: 投稿1件の結果
      case 'postResult': {
        const s = await getState();
        if (s.status !== 'running' || !s.current) { sendResponse({ ok: false }); break; }
        const buffer = [...(s.buffer || []), {
          url: s.current.url, postKey: s.current.postKey,
          text: msg.text || '', postedAt: msg.postedAt || '',
          likes: msg.likes ?? null, replies: msg.replies ?? null,
          impressions: msg.impressions ?? null, replyTexts: msg.replyTexts || [],
        }];
        await setState({ buffer, current: null });
        await advanceArchive();
        sendResponse({ ok: true });
        break;
      }

      // 異常検知（ログイン画面・確認画面・エラー）
      case 'anomaly': {
        await stopRun(`異常を検知したため停止しました: ${msg.reason || '不明'}`, true);
        sendResponse({ ok: true });
        break;
      }
      // コントローラのタブが閉じた
      case 'controllerGone': {
        const s = await getState();
        if (s.status === 'running') await stopRun('操作画面が閉じられたため停止しました', false);
        sendResponse({ ok: true });
        break;
      }
      default: sendResponse({ ok: false, error: 'unknown command' });
    }
  })();
  return true;
});

function pace(settings) {
  return {
    minDelayMs: settings.minDelayMs, maxDelayMs: settings.maxDelayMs,
    longPauseChance: settings.longPauseChance,
    longPauseMinMs: settings.longPauseMinMs, longPauseMaxMs: settings.longPauseMaxMs,
  };
}

chrome.tabs.onRemoved.addListener(async (tabId) => {
  const s = await getState();
  if (s.tabId === tabId && s.status === 'running') {
    await stopRun('作業中のタブが閉じられたため停止しました', false);
  }
});

// 旧バージョンが登録した alarms が生き残ると、新版でも意図しないタブ遷移が起きる。
// この版では alarms を使わないが、掃除のために権限は残してある（次の版で外す）。
chrome.runtime.onInstalled.addListener(async () => {
  try { await chrome.alarms?.clearAll?.(); } catch (e) { /* 権限が無ければ無視 */ }
  await chrome.storage.local.set({ state: { ...EMPTY_STATE } });
});

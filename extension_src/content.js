// Threadsバズ投稿収集 — content script
//
// ⚠️ ThreadsのDOMは頻繁に変わり、かつ実機（ログイン済みThreads）でしか検証できない。
// そのため「1つのセレクタに賭ける」のではなく、各項目（本文・いいね・返信・表示回数）を
// 複数の戦略で順番に試し、最初に取れたものを採用する方式にしている。
// さらにデバッグモード(settings.debugMode)をONにすると、Threadsの投稿ページを開くだけで
// 「各戦略が何を拾い、何を取りこぼしたか」をコンソールに出力するので、実機での調整が一気に進む。

function sleep(ms) { return new Promise((r) => setTimeout(r, ms)); }
function randInt(min, max) { return Math.floor(min + Math.random() * (max - min)); }

let DEBUG = false;
async function loadDebugFlag() {
  try {
    const { settings } = await chrome.storage.local.get(['settings']);
    DEBUG = !!(settings && settings.debugMode);
  } catch (e) { DEBUG = false; }
}
function dlog(...args) { if (DEBUG) console.log('%c[buzz-threads]', 'color:#8b5cf6;font-weight:bold', ...args); }
function dgroup(title) { if (DEBUG) console.group('%c[buzz-threads] ' + title, 'color:#8b5cf6;font-weight:bold'); }
function dgroupEnd() { if (DEBUG) console.groupEnd(); }

function getPostKeyFromHref(href) {
  const m = (href || '').match(/\/post\/([A-Za-z0-9_-]+)/);
  return m ? m[1] : null;
}

function absoluteThreadsUrl(href) {
  if (!href) return '';
  if (href.startsWith('http')) return href;
  return `https://www.threads.com${href.startsWith('/') ? '' : '/'}${href}`;
}

// ---------- 数値パース ----------
// 「1,234」「1.2万」「3.4億」「12K」「5.6M」等をintに。取れなければnull。
function parseCount(raw) {
  if (raw == null) return null;
  let s = String(raw).trim().replace(/,/g, '');
  // 末尾の単位語（件/回/表示/views/likes等）を連続して除去（例:「12.3万件の表示」→「12.3万」）
  s = s.replace(/(?:\s*(?:件|回|の表示|表示|views?|likes?|replies?|reposts?))+\s*$/i, '').trim();
  let m = s.match(/^([\d.]+)\s*万$/); if (m) return Math.round(parseFloat(m[1]) * 10000);
  m = s.match(/^([\d.]+)\s*億$/);   if (m) return Math.round(parseFloat(m[1]) * 100000000);
  m = s.match(/^([\d.]+)\s*[KkＫ]$/); if (m) return Math.round(parseFloat(m[1]) * 1000);
  m = s.match(/^([\d.]+)\s*[MmＭ]$/); if (m) return Math.round(parseFloat(m[1]) * 1000000);
  m = s.match(/^([\d.]+)\s*[BbＢ]$/); if (m) return Math.round(parseFloat(m[1]) * 1000000000);
  if (/^\d+(\.\d+)?$/.test(s)) return Math.round(parseFloat(s));
  return null;
}

// テキストから「数値＋単位」トークンを抜き出す（複数あれば最初の1つ）
function extractNumberToken(text) {
  const m = (text || '').match(/[\d,]+\.?\d*\s*(?:万|億|[KkMmBbＫＭＢ])?/);
  return m ? m[0] : null;
}

function uniq(arr) { return Array.from(new Set(arr)); }

// 投稿コンテナ内の全 aria-label を集める（デバッグ用）
function collectAriaLabels(container) {
  return uniq(
    Array.from(container.querySelectorAll('[aria-label]'))
      .map((el) => (el.getAttribute('aria-label') || '').trim())
      .filter(Boolean)
  );
}

// ---------- 数値検出（複数戦略） ----------
// pattern: その項目を表す語（いいね/返信/表示回数 等）の正規表現
// 戦略A: aria-label が pattern に一致し、ラベル自体に数字が含まれる
// 戦略B: aria-label が pattern に一致する要素の、クリック可能な祖先(button/link)や兄弟のテキストから数字
// 戦略C: ページ全体のテキストから「数字＋単位＋対象語」または「対象語＋数字」の並びを探す
function findCountMultiStrategy(container, pattern, unitWordPattern, debugName) {
  const tried = [];

  // 戦略A
  const labeled = Array.from(container.querySelectorAll('[aria-label]'))
    .filter((el) => pattern.test(el.getAttribute('aria-label') || ''));
  for (const el of labeled) {
    const label = el.getAttribute('aria-label') || '';
    const v = parseCount(extractNumberToken(label));
    tried.push({ strategy: 'A(aria-label内の数字)', label, value: v });
    if (v != null) { dlog(`  ${debugName}: 戦略Aで検出`, v, `← "${label}"`); return { value: v, via: 'A', label }; }
  }

  // 戦略B: aria-label要素→クリック可能な祖先やその近傍テキスト
  for (const el of labeled) {
    const holder = el.closest('a,button,[role="button"],[role="link"]') || el.parentElement || el;
    const scope = holder.parentElement || holder;
    const v = parseCount(extractNumberToken(scope.textContent || ''));
    tried.push({ strategy: 'B(近傍テキストの数字)', text: (scope.textContent || '').slice(0, 40), value: v });
    if (v != null) { dlog(`  ${debugName}: 戦略Bで検出`, v, `← "${(scope.textContent || '').slice(0, 40)}"`); return { value: v, via: 'B' }; }
  }

  // 戦略C: 単位語を含む「短いテキスト要素」だけを対象に数字を採る。
  // 全体テキストを正規表現にかけると隣の要素の数字を誤って拾うので、要素単位に絞る。
  if (unitWordPattern) {
    const wordRe = new RegExp(unitWordPattern, 'i');
    for (const el of container.querySelectorAll('*')) {
      const txt = (el.textContent || '').trim();
      if (!txt || txt.length > 40) continue;      // 長い塊は複数要素混在なので除外
      if (!wordRe.test(txt)) continue;             // その単位語を含む短片のみ
      const v = parseCount(extractNumberToken(txt));
      if (v != null) {
        tried.push({ strategy: 'C(単位語を含む短片)', text: txt, value: v });
        dlog(`  ${debugName}: 戦略Cで検出`, v, `← "${txt}"`);
        return { value: v, via: 'C' };
      }
    }
  }

  if (DEBUG) { dlog(`  ${debugName}: 検出失敗。試行内容:`, tried); }
  return { value: null, via: null };
}

// 表示回数（インプレッション）専用。個別投稿ページの上部に「表示3.7万回」「表示 12,345 回」等の
// 形で出る（他人の投稿でも見える。ただしフィード/検索結果には出ない）。書式が独特なので専用に拾う。
function findViewCount(root) {
  const text = (root.innerText || root.textContent || '');
  // 日本語:「表示3.7万回」「表示 12,345 回」
  let m = text.match(/表示\s*([\d.,]+\s*(?:万|億|[KkMmBb])?)\s*回/);
  if (m) { const v = parseCount(m[1]); if (v != null) return v; }
  // 英語:「3.7M views」「12,345 views」
  m = text.match(/([\d.,]+\s*(?:万|億|[KkMmBb])?)\s*views?/i);
  if (m) { const v = parseCount(m[1]); if (v != null) return v; }
  return null;
}

function extractStatsFromContainer(container) {
  const likes = findCountMultiStrategy(container, /いいね|[Ll]ikes?/, 'いいね|[Ll]ikes?', 'likes').value;
  const replies = findCountMultiStrategy(container, /返信|リプライ|[Rr]epl(y|ies)/, '返信|リプライ|[Rr]eplies?', 'replies').value;
  // まず専用の「表示◯回」検出を試し、ダメなら汎用の複数戦略にフォールバック
  let impressions = findViewCount(document.body);
  if (impressions == null) {
    impressions = findCountMultiStrategy(container, /表示|インプレッション|[Vv]iews?/, '表示|インプレッション|[Vv]iews?', 'impressions').value;
  }
  return { likes, replies, impressions };
}

// 表示回数(impressions)は他人の投稿では非公開＝取得できないことが実機で判明（2026-07-30）。
// 代わりに「いいね＋リプライ×3」で判定する。Threadsのインプレッションの約50%がリプライ由来のため、
// リプライを重く見ることで「リーチ（表示回数）が伸びた投稿」に近い基準になる。
const REPLY_WEIGHT = 3;
function scoreOf(stats) {
  return (stats.likes || 0) + (stats.replies || 0) * REPLY_WEIGHT;
}

// ---------- 本文の取得（複数戦略） ----------
// og:description は Threads だと「ユーザー名 (@handle) on Threads: 本文…」形式や
// 末尾が「…」で切れていることがあるため、前置きを除去し、DOMからの本文も候補にする。
function cleanOgDescription(desc) {
  let s = (desc || '').trim();
  // 「◯◯ (@handle) on Threads: 」「◯◯さんがThreadsに投稿しました: 」等の前置きを除去
  s = s.replace(/^.{0,60}?\(@[^)]+\)\s*(?:on Threads)?\s*[:：]\s*/i, '');
  s = s.replace(/^.{0,40}?さん.*?Threads.*?[:：]\s*/, '');
  return s.trim();
}

// 投稿ページのメイン投稿本文をDOMから拾う（og:descriptionが空/短い時のフォールバック）
function extractTextFromDom() {
  // Threadsの本文はだいたい最初の投稿ブロック内の長めのテキスト。
  // article / [data-pressable-container] / role=article を優先的に見る。
  const roots = [
    ...document.querySelectorAll('[data-pressable-container]'),
    ...document.querySelectorAll('article, [role="article"]'),
  ];
  for (const root of roots) {
    // ボタン群やユーザー名を除いた、意味のある長さのテキストを探す
    const t = (root.innerText || root.textContent || '').trim();
    if (t && t.length >= 15) return t.slice(0, 600);
  }
  return '';
}

// ---------- 著者本人のツリー（連投）の抽出 ----------
// リプライ欄には他人の投稿も混ざる。回収するのは「投稿者本人の連投」だけ。
// URLの @ハンドル で本人を特定し、DOM順に本人の投稿を集め、他人のブロックに当たったら打ち切る。
// さらに安全のため最大8投稿(本文含む)までしか追わない。
const MAX_THREAD_POSTS = 8;

function blockAuthorHandle(block) {
  const link = block.querySelector('a[href^="/@"]');
  if (!link) return '';
  const m = (link.getAttribute('href') || '').match(/^\/@([^/?]+)/);
  return m ? m[1] : '';
}

// ブロックを行の配列にする。innerText（改行あり）が使えればそれ、ダメなら末端要素のテキストを行とみなす。
function getBlockLines(block) {
  if (typeof block.innerText === 'string' && block.innerText.trim()) {
    return block.innerText.split('\n');
  }
  const lines = [];
  block.querySelectorAll('a, span, div, time, p').forEach((el) => {
    if (el.children.length === 0) {
      const t = (el.textContent || '').trim();
      if (t) lines.push(t);
    }
  });
  return lines;
}

// 投稿ブロックのテキストから、ハンドル名・相対時刻・単独数字・ボタン語などのノイズを除いて本文だけ取り出す
function extractPostTextFromBlock(block, handle) {
  const lines = getBlockLines(block).map((s) => s.trim()).filter(Boolean);
  const cleaned = lines.filter((line) => {
    if (line === handle) return false;                              // ハンドル
    if (/^\d+\s*(?:時間|分|秒|日|週|か月|年|[hmsdw])前?$/.test(line)) return false; // 相対時刻(18時間 等)
    if (/^\d{4}[/／年.\-]\s*\d{1,2}[/／月.\-]\s*\d{1,2}日?$/.test(line)) return false; // 絶対日付(2026/04/23 等)
    if (/^[\d,]+$/.test(line)) return false;                        // 単独の数字(いいね数等)
    if (line === '/') return false;                                 // 1/3 の区切り
    if (/^\d+\/\d+$/.test(line)) return false;                      // "1/3"
    if (/^(?:トップ|アクティビティを見る|返信|いいね|再投稿|シェア(?:する)?|フォロー(?:する)?|もっと見る|翻訳を見る|フォローバック)$/.test(line)) return false;
    if (/^表示[\d.,]+\s*(?:万|億)?回$/.test(line)) return false;    // 表示◯万回
    return true;
  });
  return cleaned.join(' ').replace(/\s+/g, ' ').trim();
}

// 本人の連投を配列で返す（posts[0]=本文, posts[1..]=本人リプ）。最大MAX_THREAD_POSTS件。
function extractAuthorThread() {
  const m = location.pathname.match(/\/@([^/]+)\/post\//);
  const handle = m ? m[1] : '';
  if (!handle) return { handle: '', posts: [] };

  // 各投稿は time[datetime] を1つ持つ。その最寄りの投稿ブロックをDOM順に集める。
  const blocks = [];
  const seen = new Set();
  for (const t of document.querySelectorAll('time[datetime]')) {
    const block = t.closest('[data-pressable-container]')
      || t.closest('div[role="article"], article')
      || t.parentElement;
    if (!block || seen.has(block)) continue;
    seen.add(block);
    blocks.push(block);
  }

  const posts = [];
  for (const block of blocks) {
    const author = blockAuthorHandle(block);
    if (author && author !== handle) break;   // 他人のリプに到達 → ツリー終了
    if (author !== handle) continue;          // 著者不明のブロックは飛ばす
    const text = extractPostTextFromBlock(block, handle);
    if (text && text.length >= 8) posts.push(text);
    if (posts.length >= MAX_THREAD_POSTS) break; // 最大8投稿まで
  }
  if (DEBUG) {
    dgroup('著者ツリー抽出（本人の連投のみ・最大' + MAX_THREAD_POSTS + '件）');
    dlog('著者ハンドル:', handle, ' / 検出ブロック数:', blocks.length, ' / 本人投稿数:', posts.length);
    posts.forEach((p, i) => dlog(`  ${i === 0 ? '本文' : 'リプ' + i}:`, p.slice(0, 60)));
    dgroupEnd();
  }
  return { handle, posts };
}

function normalizeForCompare(s) { return (s || '').replace(/\s+/g, '').slice(0, 50); }

function extractPostDetail() {
  const descMeta = document.querySelector('meta[property="og:description"]');
  const rawOg = descMeta ? (descMeta.getAttribute('content') || '') : '';
  const ogText = cleanOgDescription(rawOg);
  const domText = extractTextFromDom();
  // og:description を優先（本文の取得が最も安定）。空か極端に短ければ DOM から補う。
  let text = ogText;
  if (!text || text.length < 10) text = domText || ogText;

  // 本人の連投からリプライ本文を抽出（本文と重複するブロックは除外）
  // ※ stats.replies は「リプライ数(カウント)」、replyTexts は「本人リプの本文配列」で別物
  const thread = extractAuthorThread();
  const replyPosts = thread.posts.filter((p) => normalizeForCompare(p) !== normalizeForCompare(text));
  const replyTexts = replyPosts.slice(0, MAX_THREAD_POSTS - 1); // 本文を除いて最大7件

  const timeEl = document.querySelector('time[datetime]');
  const postedAt = timeEl ? (timeEl.getAttribute('datetime') || '').slice(0, 10) : '';
  const stats = extractStatsFromContainer(document.body);

  if (DEBUG) {
    dgroup('本文抽出');
    dlog('og:description(raw):', rawOg);
    dlog('採用した本文:', text);
    dlog('本人リプ本文数:', replyTexts.length, replyTexts.map((r) => r.slice(0, 30)));
    dlog('投稿日:', postedAt);
    dgroupEnd();
  }
  return { text, replyTexts, postedAt, rawOg, ogText, domText, ...stats };
}

// ---------- デバッグレポート（実機調整用） ----------
// デバッグモードONで投稿ページを開くと自動実行。DevToolsコンソールに結果が出る。
function runDebugReport() {
  dgroup('🔬 デバッグレポート ' + location.pathname);
  dlog('URL:', location.href);
  const detail = extractPostDetail();
  dgroup('検出された数値');
  dlog('いいね:', detail.likes, ' / 返信:', detail.replies, ' / 表示回数:', detail.impressions);
  if (detail.impressions == null) dlog('※ 表示回数が取れませんでした。個別投稿ページ上部の「表示◯万回」が読めているか、この時点でまだ描画されていない可能性があります。ページ上部のHTMLを共有してください。');
  dgroupEnd();
  dgroup('ページ内の全 aria-label（数値の在り処を探す手がかり）');
  dlog(collectAriaLabels(document.body));
  dgroupEnd();
  dlog('↑ 数値が取れていない場合、上の aria-label 一覧やDOM構造をこのレポートと一緒に共有してください。セレクタを合わせます。');
  dgroupEnd();
}

// ---------- オーバーレイ ----------
function createOverlay() {
  const host = document.createElement('div');
  host.id = 'buzz-threads-overlay-host';
  host.style.cssText = 'all:initial;position:fixed;bottom:16px;right:16px;z-index:2147483647;';
  const shadow = host.attachShadow({ mode: 'open' });
  const style = document.createElement('style');
  style.textContent = `
    .box { font-family: -apple-system, BlinkMacSystemFont, sans-serif; background:#2b2b2b; color:#fff;
      border-radius:10px; padding:12px 14px; width:270px; box-shadow:0 4px 16px rgba(0,0,0,.45); font-size:13px; line-height:1.6; }
    .box.anomaly { background:#7a2e2e; }
    .box.debug { background:#3a2b5a; }
    .row { margin-bottom:6px; }
    button { font-size:12px; padding:6px 10px; border-radius:6px; border:none; cursor:pointer; margin-right:6px; }
    .btn-secondary { background:#555; color:#fff; }
    .small { font-size:11px; opacity:.85; }
    .mono { font-family:ui-monospace,monospace; font-size:11px; }
  `;
  shadow.appendChild(style);
  const box = document.createElement('div');
  box.className = 'box';
  shadow.appendChild(box);
  document.documentElement.appendChild(host);
  return { host, box };
}

function removeOverlayLater(host, ms) { setTimeout(() => host.remove(), ms); }

function setupSkipButton(box, host, seconds) {
  const btn = box.querySelector('#buzz-skip');
  const countdownEl = box.querySelector('#buzz-countdown');
  let remaining = seconds;
  let done = false;
  const finish = async () => {
    if (done) return;
    done = true;
    clearInterval(timer);
    try { await chrome.runtime.sendMessage({ cmd: 'skip' }); } catch (e) { /* ignore */ }
    host.remove();
  };
  const timer = setInterval(() => {
    remaining -= 1;
    if (countdownEl) countdownEl.textContent = `${remaining}秒後に自動スキップ`;
    if (remaining <= 0) finish();
  }, 1000);
  btn.onclick = finish;
}

// ---------- 検索結果ページ: 投稿カードを発見 ----------
function scanSearchResults(threshold) {
  const links = Array.from(document.querySelectorAll('a[href*="/post/"]'));
  const seen = new Set();
  const results = [];
  for (const a of links) {
    const postKey = getPostKeyFromHref(a.getAttribute('href'));
    if (!postKey || seen.has(postKey)) continue;
    seen.add(postKey);

    const container = a.closest('[role="article"]') || a.closest('article')
      || a.closest('[data-pressable-container]')
      || a.parentElement?.parentElement?.parentElement || a.parentElement;
    if (!container) continue;

    const stats = extractStatsFromContainer(container);
    const score = scoreOf(stats);
    if (DEBUG) dlog(`検索結果カード ${postKey}: score=${score}`, stats);
    if (score < threshold) continue;

    results.push({
      postKey,
      url: absoluteThreadsUrl(a.getAttribute('href')),
      likes: stats.likes, replies: stats.replies, impressions: stats.impressions,
    });
  }
  return results;
}

async function runSearchDiscovery(threshold, alreadyProcessed) {
  const seen = new Set(alreadyProcessed || []);
  const found = new Map();
  for (let i = 0; i < 6; i++) {
    await sleep(randInt(1500, 3000));
    window.scrollBy({ top: randInt(500, 1000), behavior: 'smooth' });
    await sleep(randInt(2000, 3500));
    const tiles = scanSearchResults(threshold);
    for (const t of tiles) {
      if (seen.has(t.postKey) || found.has(t.postKey)) continue;
      found.set(t.postKey, t);
    }
    if (found.size >= 5) break;
  }
  return Array.from(found.values());
}

// ---------- メイン ----------
async function main() {
  await loadDebugFlag();

  // デバッグモード時は、収集タスク中でなくても投稿ページを開いたら自動でレポートを出す
  if (DEBUG && getPostKeyFromHref(location.pathname)) {
    // ページ描画を少し待ってからレポート
    setTimeout(runDebugReport, 1800);
  }

  let activeCheck = { active: false };
  try {
    activeCheck = await chrome.runtime.sendMessage({ cmd: 'checkActive', path: location.pathname, search: location.search });
  } catch (e) { return; }
  if (!activeCheck.active) return;

  if (activeCheck.phase === 'search') {
    const { host, box } = createOverlay();
    box.innerHTML = `<div class="row">🔍 バズ投稿をリサーチ中...</div>`;
    const candidates = await runSearchDiscovery(activeCheck.threshold, activeCheck.alreadyProcessed);
    box.innerHTML = `<div class="row">✅ ${candidates.length}件の候補を発見（スコア${activeCheck.threshold.toLocaleString()}以上）</div>`;
    removeOverlayLater(host, 3000);
    await chrome.runtime.sendMessage({ cmd: 'searchCandidates', candidates });
    return;
  }

  if (activeCheck.phase === 'post') {
    const postKey = getPostKeyFromHref(location.pathname);
    if (!postKey || postKey !== activeCheck.current.postKey) return;

    const { host, box } = createOverlay();
    box.innerHTML = `<div class="row">🔍 投稿内容を確認中...</div>`;
    await sleep(randInt(1000, 2000));

    const detail = extractPostDetail();
    const likes = detail.likes ?? activeCheck.current.likes;
    const replies = detail.replies ?? activeCheck.current.replies;   // リプライ数(カウント)
    const impressions = detail.impressions ?? activeCheck.current.impressions;
    const replyTexts = detail.replyTexts || [];                       // 本人リプの本文配列

    if (!detail.text) {
      box.classList.add('anomaly');
      box.innerHTML = `
        <div class="row">⚠️ 本文を検出できませんでした</div>
        ${DEBUG ? '<div class="row small">デバッグON: コンソールに詳細を出力しました</div>' : ''}
        <button id="buzz-skip" class="btn-secondary">スキップして次へ</button>
        <div class="row small" id="buzz-countdown">10秒後に自動スキップ</div>
      `;
      await chrome.runtime.sendMessage({ cmd: 'postAnomaly', postKey, reason: '本文検出失敗' });
      setupSkipButton(box, host, 10);
      return;
    }

    if (DEBUG) box.classList.add('debug');
    box.innerHTML = `
      <div class="row">✅ 本文取得済み</div>
      <div class="row small">👍${likes ?? '-'} ・ 💬${replies ?? '-'} ・ 👁${impressions ?? '-'}</div>
      ${DEBUG ? `<div class="row small mono">本文: ${(detail.text || '').slice(0, 40)}…</div>` : ''}
      <div class="row small">確認中…まもなく登録します</div>
    `;
    await sleep(randInt(1500, 3000));
    const res = await chrome.runtime.sendMessage({
      cmd: 'postResult', postKey, text: detail.text, postedAt: detail.postedAt,
      likes, replies, impressions, replyTexts,
    });
    box.innerHTML = res.ok
      ? `<div class="row">✅ アーカイブに登録しました</div>`
      : `<div class="row">❌ 登録失敗: ${res.error}</div>`;
    removeOverlayLater(host, res.ok ? 2500 : 6000);
  }
}

main().catch((e) => console.error('[buzz-threads-collector]', e));

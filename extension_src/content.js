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

// Threads以外のURLを弾く。プロフィール上の投稿本文に外部リンクがあると、
// それを候補に入れてタブごと外部サイトへ遷移させられてしまう。
const THREADS_ORIGINS = ['https://www.threads.com', 'https://www.threads.net'];
function isThreadsUrl(u) {
  try { return THREADS_ORIGINS.includes(new URL(u).origin); } catch (e) { return false; }
}
// ハンドル名はサーバ側 /ingest と同じ形式に縛る。
// レポート画面が innerHTML で描画するので、ここを通さないと属性の脱出を許す。
const HANDLE_RE = /^[A-Za-z0-9_.]{1,30}$/;
function safeHandle(h) { return (h && HANDLE_RE.test(h)) ? h : null; }

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

// 【訂正 2026-08-06】以前ここに「表示回数は他人の投稿では取得できない」と書いてあったが誤り。
// 投稿の詳細ページを開けば他人の投稿でも取得できる（本番171件中170件で取得済み・実測）。
// 取得できないのは検索結果・フィードの一覧カードだけで、その場合は投稿ページを開く必要がある。
// この誤ったコメントを根拠に設計を誤ったことがあるので、消さずに訂正として残す。
//
// REPLY_WEIGHT は一覧カードでの暫定スコア用。詳細ページを開けば表示回数が取れるので、
// 本来の指標は「いいね率＝いいね÷表示回数」。
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


// ===========================================================================
// 2モード構成のコンテンツ側。
//   discover : 検索結果をゆっくりスクロールし、アカウント単位で いいね/リプライ を集計
//   profile  : アカウントページから投稿リンクとフォロワー数を集める
//   post     : 投稿の詳細ページから本文・数値（表示回数を含む）を取る
//
// 待機はすべてここで行う。background の chrome.alarms は MV3 で30秒未満が
// 丸められるため、人間的な揺らぎ（4〜11秒）を作れない。
//
// 各ステップの前に、コントローラ（report.html）のハートビートを確認する。
// 画面を閉じられていたら、そこで止まる。
// ===========================================================================

// 裏に回ったタブは Chrome が setInterval を1分間隔まで絞るため、3秒だと誤停止する。
// タブを閉じたときは report 側が pagehide で 0 を書くので、この値を延ばしても即座に止まる。
const HEARTBEAT_MAX_AGE_MS = 90000;

async function controllerAlive() {
  try {
    const d = await chrome.storage.local.get(['controllerHeartbeat']);
    return (Date.now() - (d.controllerHeartbeat || 0)) < HEARTBEAT_MAX_AGE_MS;
  } catch (e) { return false; }
}

// 操作間隔。たまに長い休止を挟む
async function paceWait(pace) {
  const long = Math.random() < (pace.longPauseChance ?? 0.1);
  const ms = long
    ? randInt(pace.longPauseMinMs ?? 20000, pace.longPauseMaxMs ?? 40000)
    : randInt(pace.minDelayMs ?? 4000, pace.maxDelayMs ?? 11000);
  dlog(`待機 ${(ms / 1000).toFixed(1)}秒${long ? '（長い休止）' : ''}`);
  await sleep(ms);
}

// ログイン要求・確認画面・エラーを検知したら止める
function detectAnomaly() {
  const t = (document.body.innerText || '').slice(0, 3000);
  if (/ログイン|Log in to Threads|Sign up/i.test(t) && !document.querySelector('a[href*="/post/"]')) {
    return 'ログイン画面が表示されました';
  }
  if (/一時的に制限|しばらくしてからもう一度|Please wait a few minutes|Try again later|rate limit/i.test(t)) {
    return '利用制限の画面が表示されました';
  }
  return null;
}

async function bail(reason) {
  dlog('中断:', reason);
  try { await chrome.runtime.sendMessage({ cmd: 'anomaly', reason }); } catch (e) { /* noop */ }
}

// 一覧カードの数値。**findViewCount(document.body) を呼ばないこと。**
// 検索結果ではページ全体から最初の「表示◯回」を拾ってしまい、全カードに同じ値が入る。
// 一覧に表示回数は出ないので、ここでは いいね と リプライ だけを取る。
function cardStats(container) {
  return {
    likes: findCountMultiStrategy(container, /いいね|[Ll]ikes?/, 'いいね|[Ll]ikes?', 'likes').value,
    replies: findCountMultiStrategy(container, /返信|リプライ|[Rr]epl(y|ies)/, '返信|リプライ|[Rr]eplies?', 'replies').value,
  };
}

function eachCard() {
  const seen = new Set();
  const out = [];
  for (const a of document.querySelectorAll('a[href*="/post/"]')) {
    const href = a.getAttribute('href') || '';
    const postKey = getPostKeyFromHref(href);
    if (!postKey || seen.has(postKey)) continue;
    seen.add(postKey);
    const container = a.closest('[role="article"]') || a.closest('article')
      || a.closest('[data-pressable-container]');
    if (!container) continue;   // 祖先を遡らない（UIテキストの混入を防ぐ）
    const handle = safeHandle((href.match(/\/@([^/]+)\//) || [])[1] || null);
    const url = absoluteThreadsUrl(href);
    if (!isThreadsUrl(url)) continue;   // Threads以外へは絶対に遷移させない
    out.push({ postKey, handle, url, container });
  }
  return out;
}

async function humanScroll() {
  const h = window.innerHeight;
  const amount = Math.round(h * (0.7 + Math.random() * 0.9));
  window.scrollBy({ top: amount, behavior: 'smooth' });
  await sleep(randInt(600, 1400));
  if (Math.random() < 0.15) {           // たまに少し戻る
    window.scrollBy({ top: -Math.round(h * 0.3), behavior: 'smooth' });
    await sleep(randInt(500, 1000));
  }
}

// ---------- モード1: アカウント発見 ----------
async function runDiscover(cfg) {
  const { host, box } = createOverlay();
  const byHandle = {};
  let scrolls = 0;
  while (scrolls < cfg.scrollMax) {
    if (!(await controllerAlive())) {
      box.innerHTML = '<div class="row">操作画面が閉じられたため停止しました</div>';
      try { await chrome.runtime.sendMessage({ cmd: 'controllerGone' }); } catch (e) { /* noop */ }
      removeOverlayLater(host, 3000); return;
    }
    const bad = detectAnomaly();
    if (bad) { await bail(bad); removeOverlayLater(host, 3000); return; }

    let added = 0;
    for (const c of eachCard()) {
      if (!c.handle) continue;
      const st = cardStats(c.container);
      if (st.likes == null || st.replies == null) continue;
      (byHandle[c.handle] ||= []);
      if (byHandle[c.handle].some((x) => x.postKey === c.postKey)) continue;
      byHandle[c.handle].push({ postKey: c.postKey, likes: st.likes, replies: st.replies });
      added += 1;
    }
    const total = Object.values(byHandle).reduce((n, a) => n + a.length, 0);
    box.innerHTML = `<div class="row">アカウントを調査中… ${Object.keys(byHandle).length}件 / 投稿 ${total}件</div>`;
    dlog(`スクロール${scrolls + 1}: 新規${added}件`);

    await paceWait(cfg.pace);
    await humanScroll();
    scrolls += 1;
  }
  try {
    await chrome.runtime.sendMessage({ cmd: 'discoverSamples', byHandle });
    await chrome.runtime.sendMessage({ cmd: 'discoverDone' });
  } catch (e) { /* noop */ }
  box.innerHTML = `<div class="row">調査completed。操作画面で結果を確認してください</div>`;
  removeOverlayLater(host, 4000);
}

// ---------- モード2-1: プロフィールから投稿一覧とフォロワー数 ----------
function parseFollowersFromOg() {
  const og = document.querySelector('meta[property="og:description"]');
  const c = og && og.content ? og.content : '';
  // 例: 「フォロワー569.7万人・スレッド153 件・…」/「5.6M followers」
  let m = c.match(/フォロワー\s*([\d.,]+\s*(?:万|億)?)\s*人/);
  if (!m) m = c.match(/([\d.,]+\s*[KkMmBb]?)\s*followers?/i);
  return m ? parseCount(m[1]) : null;
}

async function runProfile(cfg) {
  const { host, box } = createOverlay();
  await sleep(randInt(1500, 2600));
  const followers = parseFollowersFromOg();
  const found = new Map();
  let scrolls = 0;
  while (found.size < cfg.postMax && scrolls < 25) {
    if (!(await controllerAlive())) {
      try { await chrome.runtime.sendMessage({ cmd: 'controllerGone' }); } catch (e) { /* noop */ }
      removeOverlayLater(host, 3000); return;
    }
    const bad = detectAnomaly();
    if (bad) { await bail(bad); removeOverlayLater(host, 3000); return; }

    for (const c of eachCard()) {
      if (found.size >= cfg.postMax) break;
      if (!found.has(c.postKey)) found.set(c.postKey, { postKey: c.postKey, url: c.url });
    }
    box.innerHTML = `<div class="row">投稿を探しています… ${found.size}/${cfg.postMax}件</div>`;
    if (found.size >= cfg.postMax) break;
    await paceWait(cfg.pace);
    await humanScroll();
    scrolls += 1;
  }
  try {
    await chrome.runtime.sendMessage({
      cmd: 'profileResult', followers, candidates: Array.from(found.values()),
    });
  } catch (e) { /* noop */ }
  box.innerHTML = `<div class="row">${found.size}件の投稿を確認します</div>`;
  removeOverlayLater(host, 2500);
}

// ---------- モード2-2: 投稿の詳細 ----------
async function runPost(cfg) {
  const { host, box } = createOverlay();
  box.innerHTML = '<div class="row">投稿の内容を確認中…</div>';
  await paceWait(cfg.pace);
  if (!(await controllerAlive())) {
    try { await chrome.runtime.sendMessage({ cmd: 'controllerGone' }); } catch (e) { /* noop */ }
    removeOverlayLater(host, 2000); return;
  }
  const bad = detectAnomaly();
  if (bad) { await bail(bad); removeOverlayLater(host, 3000); return; }

  const d = extractPostDetail();
  try {
    await chrome.runtime.sendMessage({
      cmd: 'postResult',
      text: d.text || '', postedAt: d.postedAt || '',
      likes: d.likes ?? null, replies: d.replies ?? null,
      impressions: d.impressions ?? null, replyTexts: d.replyTexts || [],
    });
  } catch (e) { /* noop */ }
  box.innerHTML = `<div class="row">表示${d.impressions ?? '—'} / いいね${d.likes ?? '—'}</div>`;
  removeOverlayLater(host, 1800);
}

// ---------- メイン ----------
async function main() {
  await loadDebugFlag();
  if (DEBUG && getPostKeyFromHref(location.pathname)) setTimeout(runDebugReport, 1800);

  let cfg = { active: false };
  try {
    cfg = await chrome.runtime.sendMessage({
      cmd: 'checkActive', path: location.pathname, search: location.search,
    });
  } catch (e) { return; }
  if (!cfg || !cfg.active) return;
  if (!(await controllerAlive())) return;   // 操作画面が閉じていたら何もしない

  if (cfg.phase === 'discover') await runDiscover(cfg);
  else if (cfg.phase === 'profile') await runProfile(cfg);
  else if (cfg.phase === 'post') await runPost(cfg);
}

main();

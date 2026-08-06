// レポート画面＝コントローラ。
// この画面が1秒ごとに chrome.storage へハートビートを書き、コンテンツスクリプトは
// 各ステップの前に「直近90秒以内か」を見る。タブを閉じるとハートビートが止まり、巡回も止まる。
// ポップアップをコントローラにしないのは、フォーカスを失うと即座に閉じてしまい、
// 5〜10分かかるモード2の実行中にユーザーが他タブを触れなくなるため。

const HEARTBEAT_MS = 1000;
// Chrome は5分以上隠れたタブのタイマーを1分間隔まで絞る。
// コンテンツ側の許容（90秒）はそれを見込んだ値にしてある。
// タブを閉じた場合は pagehide で即座に 0 を書くので、待たずに止まる。

function $(id) { return document.getElementById(id); }
function fmt(n) { return (n == null) ? '—' : Number(n).toLocaleString(); }
// innerHTML に流す値は必ず通す。handle は Threads のDOM由来、label はサーバ由来で、
// どちらも文字種が保証されていない。
function esc(v) {
  return String(v ?? '').replace(/[&<>"']/g, (c) => (
    { '&': '&amp;', '<': '&lt;', '>': '&gt;', '"': '&quot;', "'": '&#39;' }[c]));
}
function pct(v) { return (v == null) ? '—' : (v * 100).toFixed(0) + '%'; }

async function send(cmd, payload = {}) {
  return chrome.runtime.sendMessage({ cmd, ...payload });
}

// ---- ハートビート ----
async function beat() {
  await chrome.storage.local.set({ controllerHeartbeat: Date.now() });
}
setInterval(beat, HEARTBEAT_MS);
beat();

// タブを閉じる/リロードするときは即座に無効化する（90秒待たずに止まる）
window.addEventListener('pagehide', () => {
  try { chrome.storage.local.set({ controllerHeartbeat: 0 }); } catch (e) { /* 閉鎖中 */ }
});

// ---- 保存先アカウント一覧 ----
async function loadAccounts() {
  const sel = $('accountSel');
  try {
    const res = await send('listAccounts');
    if (!res || !res.ok) {
      sel.innerHTML = '<option value="">取得できませんでした</option>';
      return;
    }
    const opts = ['<option value="">保存先を選択…</option>']
      .concat((res.accounts || []).map((a) => `<option value="${esc(a.id)}">${esc(a.label)}</option>`));
    sel.innerHTML = opts.join('');
  } catch (e) {
    sel.innerHTML = '<option value="">取得できませんでした</option>';
  }
}

// ---- レポート描画 ----
function renderReport(accounts) {
  const area = $('reportArea');
  const list = Object.values(accounts || {});
  // 「いいね100以上の投稿を3件以上見た」アカウントだけを対象にする。
  // いいね数で並べるとフォロワー数と拡散運に引きずられるため、既定は返信率。
  const HANDLE_RE = /^[A-Za-z0-9_.]{1,30}$/;
  const ready = list.filter((a) => HANDLE_RE.test(a.handle || '')
    && (a.samples || []).filter((s) => s.likes >= 100).length >= 3);
  $('acctCount').textContent = `${ready.length}件`;
  if (!ready.length) {
    area.innerHTML = `<div class="empty">条件を満たすアカウントがまだありません。<br>
      （いいね100以上の投稿を3件以上見たアカウントが対象です。今 ${list.length} アカウントを観測中）</div>`;
    return;
  }
  ready.forEach((a) => {
    const s = a.samples.filter((x) => x.likes >= 100);
    a._likes = median(s.map((x) => x.likes));
    a._replies = median(s.map((x) => x.replies));
    a._rate = median(s.map((x) => (x.likes > 0 ? x.replies / x.likes : 0)));
    a._n = s.length;
  });
  ready.sort((x, y) => y._rate - x._rate);
  area.innerHTML = `<table><thead><tr>
      <th>アカウント</th><th class="num">見た数</th><th class="num">いいね</th>
      <th class="num">リプライ</th><th class="num">返信率</th><th class="num">フォロワー</th><th></th>
    </tr></thead><tbody>` +
    ready.map((a) => `<tr>
      <td><a href="https://www.threads.com/@${encodeURIComponent(a.handle)}" target="_blank" rel="noopener noreferrer">@${esc(a.handle)}</a></td>
      <td class="num">${a._n}</td>
      <td class="num">${fmt(a._likes)}</td>
      <td class="num">${fmt(a._replies)}</td>
      <td class="num rate">${pct(a._rate)}</td>
      <td class="num">${fmt(a.followers)}</td>
      <td><button class="btn sub pick" data-h="${esc(a.handle)}">これを集める</button></td>
    </tr>`).join('') + '</tbody></table>';
  area.querySelectorAll('.pick').forEach((b) => {
    b.addEventListener('click', () => {
      $('profileUrl').value = `https://www.threads.com/@${b.dataset.h}`;
      $('profileUrl').scrollIntoView({ behavior: 'smooth', block: 'center' });
      $('accountSel').focus();
    });
  });
}

function median(a) {
  if (!a || !a.length) return null;
  const s = [...a].sort((x, y) => x - y);
  const m = Math.floor(s.length / 2);
  return s.length % 2 ? s[m] : (s[m - 1] + s[m]) / 2;
}

// ---- 状態の反映 ----
async function refresh() {
  const st = await send('getStatus');
  if (!st) return;
  const box = $('status');
  const running = st.status === 'running';
  box.className = running ? 'run' : (st.status === 'stopped_error' ? 'stop' : '');
  box.textContent = st.statusText || (running ? '実行中' : '待機中');
  $('stop1').style.display = running && st.mode === 'discover' ? '' : 'none';
  $('stop2').style.display = running && st.mode === 'archive' ? '' : 'none';
  $('startDiscover').disabled = running;
  $('startArchive').disabled = running;
  $('log').textContent = (st.log || []).slice(-8).join('\n') || '—';
  const data = await chrome.storage.local.get(['report']);
  renderReport(data.report || {});
}

$('startDiscover').addEventListener('click', async () => {
  const kw = $('kw').value.trim();
  if (!kw) { alert('キーワードを入れてください'); return; }
  await beat();
  await send('startDiscover', { keyword: kw });
  refresh();
});

$('startArchive').addEventListener('click', async () => {
  const url = $('profileUrl').value.trim();
  const accountId = $('accountSel').value;
  if (!/^https:\/\/www\.threads\.(com|net)\/@[^/]+/.test(url)) {
    alert('アカウントのURLを入れてください（例: https://www.threads.com/@handle）'); return;
  }
  if (!accountId) { alert('保存先のアカウントを選んでください'); return; }
  await beat();
  await send('startArchive', { profileUrl: url, accountId });
  refresh();
});

$('stop1').addEventListener('click', async () => { await send('stop'); refresh(); });
$('stop2').addEventListener('click', async () => { await send('stop'); refresh(); });
$('clearReport').addEventListener('click', async () => {
  if (!confirm('集めたアカウントのレポートを消します。よろしいですか？')) return;
  await chrome.storage.local.set({ report: {} });
  refresh();
});

loadAccounts();
refresh();
setInterval(refresh, 2000);

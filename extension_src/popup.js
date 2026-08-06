// ポップアップは「操作画面を開く」「停止」だけを担う。
// 実行の司令塔は report.html（タブ）側。ポップアップはフォーカスを失うと即閉じるため、
// 5〜10分かかる収集のコントローラには使えない。
async function refresh() {
  try {
    const s = await chrome.runtime.sendMessage({ cmd: 'getStatus' });
    const el = document.getElementById('st');
    el.textContent = (s && s.statusText) || (s && s.status === 'running' ? '実行中' : '待機中');
    el.className = s && s.status === 'running' ? 'run' : '';
  } catch (e) { /* noop */ }
}
document.getElementById('open').addEventListener('click', async () => {
  await chrome.runtime.sendMessage({ cmd: 'openController' });
  window.close();
});
document.getElementById('stop').addEventListener('click', async () => {
  await chrome.runtime.sendMessage({ cmd: 'stop' });
  refresh();
});
refresh();
setInterval(refresh, 1500);

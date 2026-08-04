"""Railwayのcronサービスから定期実行されるスクリプト。

Volumeは1サービスにしか接続できないため、このプロセスからDBは読めない。
Web側の /api/cron/run-due を1回叩いて、結果を出力して即終了する。
（終了しないと次回のcron実行がスキップされるため、後処理を残さない）

【このファイルは cron/cron_tick.py と同じ内容を保つこと】
Railwayのcronサービスは Root Directory が cron/ なので実体は cron/ 側。
リポジトリ直下のこのファイルは、Root Directory 未設定のサービス用に残してある。
片方だけ直すと本番の挙動が変わらないので必ず両方直す。

【終了コードの方針】
Railwayは「終了コード0＝成功／それ以外＝クラッシュ」で判定し、
0以外だと利用者に "Deploy Crashed!" メールを送る。
そのため終了コードは「人が直さないと直らないか」で決める。

  1 を返す（人に気づかせる）:
      環境変数の未設定 / 401・403（合言葉違い）/ 500（アプリのバグ）
      /api/cron/run-due の 503（CRON_SHARED_KEY未設定のfail-closed）
      URLの書式不正（スキーマ忘れなど。5分後も直らない）

  0 を返す（次回の実行で勝手に直る）:
      404・502・504、およびRailwayエッジが返す503
      （デプロイ直後はwebの起動前にcronが発火することがあり、
        エッジが `{"status":"error","code":404,...}` を返す。実測済み）
      接続失敗・タイムアウト

0 を返すと Railway 側では異常が見えなくなるため、web が
「最後にcronが成功した時刻」を記録し、画面側で滞留を知らせている。
"""

import os
import sys
import time

import requests

ARCHIVE_URL = os.environ.get('ARCHIVE_URL', '').rstrip('/')
CRON_KEY = os.environ.get('CRON_SHARED_KEY', '')
# サーバ側は120秒で新規の確保をやめるが、投稿中のツリーはそれより伸びることがある。
# read timeoutは異常ではないので、後段で警告扱いにする。
TIMEOUT_SEC = 170
CONNECT_RETRIES = 2
RETRY_WAIT_SEC = 10

# 人が直さないと直らないHTTPステータス
HARD_FAIL_STATUS = {401, 403, 500}
# 起動途中・再起動中に出る。次回の実行で直る
TRANSIENT_STATUS = {404, 408, 425, 429, 502, 503, 504}


def _warn(msg):
    print(f'WARN: {msg}', file=sys.stderr)


def _classify_response(res):
    """レスポンスから終了コードを決める。"""
    if res.ok:
        return 0
    code = res.status_code
    body = (res.text or '')[:200]

    # 自前のfail-closed（CRON_SHARED_KEY未設定）。エッジの503と区別するため本文で見る
    if code == 503 and 'cron disabled' in body:
        print('ERROR: web側で CRON_SHARED_KEY が未設定です（cron disabled）', file=sys.stderr)
        return 1
    if code in HARD_FAIL_STATUS:
        print(f'ERROR: status={code} 設定またはアプリ側の問題です。'
              f'放置しても直りません', file=sys.stderr)
        return 1
    if code in TRANSIENT_STATUS:
        _warn(f'status={code} webがまだ起動していない可能性があります。'
              f'次回（5分後）の実行で再試行します')
        return 0
    # 未知の4xxは実装の問題として扱う
    print(f'ERROR: 想定外の status={code}', file=sys.stderr)
    return 1


def main():
    if not ARCHIVE_URL or not CRON_KEY:
        # キーそのものは出力しない
        print('ERROR: ARCHIVE_URL / CRON_SHARED_KEY が設定されていません', file=sys.stderr)
        return 1

    url = f'{ARCHIVE_URL}/api/cron/run-due'
    for attempt in range(CONNECT_RETRIES + 1):
        try:
            res = requests.post(url, headers={'X-Cron-Key': CRON_KEY}, timeout=TIMEOUT_SEC)
            print(f'status={res.status_code} body={res.text[:300]}')
            return _classify_response(res)

        except (requests.exceptions.MissingSchema,
                requests.exceptions.InvalidSchema,
                requests.exceptions.InvalidURL):
            # ARCHIVE_URL の書式が違う。5分後も直らないので気づかせる
            print('ERROR: ARCHIVE_URL の書式が正しくありません。'
                  'https:// から始まる形で設定してください', file=sys.stderr)
            return 1

        except requests.exceptions.ConnectionError as e:
            # 注意: requests は「送信後に接続が切れた場合」も ConnectionError を投げる。
            # つまり届いていない保証はないので、リトライ回数を増やすときは
            # サーバ側のロック（status='posting' へのアトミックなUPDATE）が
            # 二重投稿を止めていることが前提になる。
            if attempt < CONNECT_RETRIES:
                _warn(f'接続失敗（{attempt + 1}回目）。{RETRY_WAIT_SEC}秒後に再試行します')
                time.sleep(RETRY_WAIT_SEC)
                continue
            _warn(f'接続できませんでした（{type(e).__name__}）。'
                  f'次回（5分後）の実行で再試行します')
            return 0

        except requests.exceptions.ReadTimeout:
            # リクエストは届いており、サーバ側は処理を続けている。
            # 投稿は非冪等なので再送しない。次回のcronが続きを処理する。
            _warn('応答待ちがタイムアウトしました（処理はサーバ側で継続中）。'
                  '次回の実行で続きを処理します')
            return 0

        except requests.exceptions.RequestException as e:
            # 上で拾えなかったもの（SSLエラー等）。次回に賭ける
            _warn(f'リクエストに失敗しました（{type(e).__name__}）。'
                  f'次回の実行で再試行します')
            return 0

    return 0


if __name__ == '__main__':
    sys.exit(main())

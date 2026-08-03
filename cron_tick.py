"""Railwayのcronサービスから定期実行されるスクリプト。

Volumeは1サービスにしか接続できないため、このプロセスからDBは読めない。
Web側の /api/cron/run-due を1回叩いて、結果を出力して即終了する。
（終了しないと次回のcron実行がスキップされるため、後処理を残さない）
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
            return 0 if res.ok else 1
        except requests.exceptions.ConnectionError as e:
            # 接続できていない = サーバに届いていないので、投稿が二重になる心配はない
            if attempt < CONNECT_RETRIES:
                print(f'接続失敗（{attempt + 1}回目）。{RETRY_WAIT_SEC}秒後に再試行します', file=sys.stderr)
                time.sleep(RETRY_WAIT_SEC)
                continue
            print(f'ERROR: 接続に失敗しました: {type(e).__name__}', file=sys.stderr)
            return 1
        except requests.exceptions.ReadTimeout:
            # リクエストは届いており、サーバ側は処理を続けている。
            # 投稿は非冪等なので再送しない。次回のcronが続きを処理する。
            print('WARN: 応答待ちがタイムアウトしました（処理はサーバ側で継続中）。'
                  '次回の実行で続きを処理します', file=sys.stderr)
            return 0

    return 1


if __name__ == '__main__':
    sys.exit(main())

"""1ログインアカウントの中に複数のThreadsアカウントを持てるようにする移行スクリプト。

これまでは「ログインアカウント = Threadsアカウント1つ」で、Threadsの認証情報は
user_settings に1組だけ入っていた。これを threads_accounts テーブルに切り出し、
予約投稿・下書き・参照投稿・収集素材を account_id で分ける。

アプリ起動時(_init_db)ではなく独立スクリプトにする理由は migrate_posts.py と同じ:
worker が2本あるので起動時に走らせると競合する／ロールバック中に再実行される／段階実行できない。

使い方:
    railway ssh --project <id> --environment production --service buzz-threads-archive \
      -- python migrate_accounts.py --dry-run
    railway ssh ... -- python migrate_accounts.py
"""

import argparse
import datetime
import os
import secrets
import sqlite3
import sys

DB_PATH = os.environ.get('DB_PATH', os.path.join(os.path.dirname(__file__), 'data', 'threads.db'))

SCOPED_TABLES = ('schedule_posts', 'drafts', 'manual_references', 'posts')

THREADS_ACCOUNTS_SCHEMA = """
CREATE TABLE IF NOT EXISTS threads_accounts (
    id TEXT PRIMARY KEY,
    username TEXT NOT NULL,
    label TEXT NOT NULL,
    threads_user_id TEXT,
    threads_access_token TEXT,
    keywords TEXT,
    sort_order INTEGER NOT NULL DEFAULT 0,
    created_at TEXT NOT NULL
)
"""


def _columns(conn, table):
    return [r[1] for r in conn.execute(f'PRAGMA table_info({table})').fetchall()]


def _table_exists(conn, table):
    return conn.execute(
        "SELECT 1 FROM sqlite_master WHERE type='table' AND name=?", (table,)
    ).fetchone() is not None


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument('--dry-run', action='store_true',
                    help='件数と実行予定の内容を表示するだけで、一切書き込まない')
    args = ap.parse_args()

    if not os.path.exists(DB_PATH):
        print(f'ERROR: DBが見つかりません: {DB_PATH}', file=sys.stderr)
        return 1

    conn = sqlite3.connect(DB_PATH, timeout=30)
    conn.row_factory = sqlite3.Row
    try:
        # 既存のThreads認証情報を持つユーザーを洗い出す。
        # これが「移行後に最初のアカウントとして作られる」対象。
        owners = {}
        for r in conn.execute(
            "SELECT username, key, value FROM user_settings "
            "WHERE key IN ('threads_user_id','threads_access_token') "
            "AND COALESCE(value,'') != ''"
        ):
            owners.setdefault(r['username'], {})[r['key']] = r['value']

        # 完了判定は「テーブルと列がある」ではなく「対象ユーザーのアカウントが作られている」で行う。
        # 列とテーブルは _init_db がアプリ起動時に作るため、それを条件にすると
        # データ割り当て前に「移行済み」と誤判定してしまう。
        if _table_exists(conn, 'threads_accounts') and all(
                'account_id' in _columns(conn, t) for t in SCOPED_TABLES):
            pending = [u for u in owners if not conn.execute(
                'SELECT 1 FROM threads_accounts WHERE username = ?', (u,)).fetchone()]
            if not pending:
                n = conn.execute('SELECT COUNT(*) FROM threads_accounts').fetchone()[0]
                print(f'移行済みです（threads_accounts が {n} 件、'
                      f'Threads設定を持つ全ユーザーにアカウントがあります）。何もしません。')
                return 0

        # 割り当て対象は owners のユーザーの行だけ。テーブル全体の件数を出すと
        # 複数ユーザーになったとき dry-run が過大表示になり、検証が成立しなくなる。
        if owners:
            ph = ','.join('?' * len(owners))
            counts = {t: conn.execute(
                f'SELECT COUNT(*) FROM {t} WHERE username IN ({ph})', list(owners)
            ).fetchone()[0] for t in SCOPED_TABLES}
            others = {t: conn.execute(
                f'SELECT COUNT(*) FROM {t} WHERE username NOT IN ({ph})', list(owners)
            ).fetchone()[0] for t in SCOPED_TABLES}
        else:
            counts = {t: 0 for t in SCOPED_TABLES}
            others = {t: conn.execute(f'SELECT COUNT(*) FROM {t}').fetchone()[0] for t in SCOPED_TABLES}
        all_users = [r[0] for r in conn.execute('SELECT username FROM users')]

        print(f'DB              : {DB_PATH}')
        print(f'ユーザー数      : {len(all_users)}')
        print(f'Threads設定あり : {len(owners)} 件 → 同数の threads_accounts を作成します')
        for t, c in counts.items():
            extra = f'（対象外ユーザーの行が {others[t]} 件残ります）' if others[t] else ''
            print(f'  {t:20s}: {c} 件に account_id を割り当て{extra}')
        print('実行内容        : threads_accounts を作成 → 各テーブルに account_id 列を追加 → '
              '既存データを最初のアカウントに割り当て')
        print('                  user_settings のトークンは削除せず残します（ロールバック用）')

        if args.dry_run:
            print('\n[dry-run] 書き込みはしていません。')
            return 0

        conn.isolation_level = None
        conn.execute('BEGIN IMMEDIATE')
        # ロック取得後に再チェック（他プロセスが先に完了している可能性）
        if all(conn.execute('SELECT 1 FROM threads_accounts WHERE username = ?', (u,)).fetchone()
               for u in owners) and owners:
            conn.execute('ROLLBACK')
            print('他のプロセスが先に移行を完了しました。何もしません。')
            return 0

        conn.execute(THREADS_ACCOUNTS_SCHEMA)
        conn.execute('CREATE INDEX IF NOT EXISTS idx_threads_accounts_username '
                     'ON threads_accounts(username)')
        for t in SCOPED_TABLES:
            if 'account_id' not in _columns(conn, t):
                conn.execute(f'ALTER TABLE {t} ADD COLUMN account_id TEXT')
            conn.execute(f'CREATE INDEX IF NOT EXISTS idx_{t}_account ON {t}(account_id)')

        now = datetime.datetime.utcnow().isoformat()
        created = 0
        for username, vals in owners.items():
            # 同じユーザーで既にアカウントがあればスキップ（再実行時の二重作成を防ぐ）
            if conn.execute('SELECT 1 FROM threads_accounts WHERE username = ?',
                            (username,)).fetchone():
                continue
            acc_id = secrets.token_hex(8)
            conn.execute(
                'INSERT INTO threads_accounts '
                '(id, username, label, threads_user_id, threads_access_token, keywords, sort_order, created_at) '
                'VALUES (?, ?, ?, ?, ?, ?, ?, ?)',
                (acc_id, username, 'メインアカウント',
                 vals.get('threads_user_id', ''), vals.get('threads_access_token', ''),
                 '', 0, now),
            )
            created += 1
            for t in SCOPED_TABLES:
                conn.execute(
                    f'UPDATE {t} SET account_id = ? WHERE username = ? AND account_id IS NULL',
                    (acc_id, username),
                )

        # 割り当て結果を検証。Threads設定を持つユーザーの行が未割り当てで残っていたら異常。
        leftovers = {}
        for t in SCOPED_TABLES:
            n = conn.execute(
                f'SELECT COUNT(*) FROM {t} WHERE account_id IS NULL AND username IN '
                f'(SELECT username FROM threads_accounts)'
            ).fetchone()[0]
            leftovers[t] = n
        if any(leftovers.values()):
            conn.execute('ROLLBACK')
            print(f'ERROR: 割り当てられなかった行があります: {leftovers}。ロールバックしました。',
                  file=sys.stderr)
            return 1

        conn.execute('COMMIT')
        print(f'\n移行完了: threads_accounts を {created} 件作成しました')
        for t in SCOPED_TABLES:
            n = conn.execute(f'SELECT COUNT(*) FROM {t} WHERE account_id IS NOT NULL').fetchone()[0]
            print(f'  {t:20s}: {n}/{counts[t]} 件に account_id を割り当て')
        print(f'完了時刻: {now}Z')
        return 0
    finally:
        conn.close()


if __name__ == '__main__':
    sys.exit(main())

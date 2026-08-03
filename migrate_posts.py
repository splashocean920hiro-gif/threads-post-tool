"""postsテーブルをユーザー単位に分離するワンショット移行スクリプト。

なぜアプリ起動時(_init_db)ではなく独立スクリプトにするか:
  - gunicornのworkerが2本あるため、起動時に走らせると同時実行で起動ループになる
  - ロールバック中に勝手に再実行され、戻した内容が無言で取り消される
  - 「移行 → 件数確認 → コードデプロイ」と段階を分けられない

使い方（本番）:
    railway ssh --project <id> --environment production --service buzz-threads-archive \
      -- python migrate_posts.py --dry-run
    railway ssh ... -- python migrate_posts.py

`railway run` はローカル実行なので本番DBには届かない。必ず `railway ssh` を使うこと。
"""

import argparse
import datetime
import os
import sqlite3
import sys

DB_PATH = os.environ.get('DB_PATH', os.path.join(os.path.dirname(__file__), 'data', 'threads.db'))
SEED_USERNAME = os.environ.get('SEED_USERNAME', 'admin')

# 移行後のスキーマ。
#   - id は AUTOINCREMENT のまま維持する（/api/reference-posts が SELECT * で id を返すため）
#   - 一意性を post_key 単独 → (post_key, username) に変更する。これが分離の本体で、
#     同じ投稿を別のユーザーが収集しても別行として保存される
NEW_SCHEMA = """
CREATE TABLE posts (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    post_key TEXT NOT NULL,
    username TEXT NOT NULL,
    url TEXT NOT NULL,
    author TEXT,
    text TEXT,
    posted_at TEXT,
    likes INTEGER,
    reply_count INTEGER,
    impressions INTEGER,
    keyword TEXT,
    first_seen_at TEXT NOT NULL,
    last_seen_at TEXT NOT NULL,
    replies_json TEXT,
    UNIQUE(post_key, username)
)
"""

# 列名を両側で明示する。posts_legacy は replies_json が ALTER で後付けされていて
# 列順が末尾になっているため、位置ベースのコピーだと無言で列がずれる。
COLUMNS = ('id', 'post_key', 'url', 'author', 'text', 'posted_at', 'likes',
           'reply_count', 'impressions', 'keyword', 'first_seen_at', 'last_seen_at', 'replies_json')


def _columns(conn, table):
    return [r[1] for r in conn.execute(f'PRAGMA table_info({table})').fetchall()]


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
        cols = _columns(conn, 'posts')
        if 'username' in cols:
            print('移行済みです（posts に username 列があります）。何もしません。')
            return 0

        total = conn.execute('SELECT COUNT(*) FROM posts').fetchone()[0]

        # SEED_USERNAME が users に存在しないと、誰からも見えない posts を作ってしまう。
        # （APP_PASSWORD 未設定だと _seed_default_user が何もしないため実際に起こりうる）
        seed = conn.execute('SELECT username FROM users WHERE username = ?', (SEED_USERNAME,)).fetchone()
        if not seed:
            names = [r[0] for r in conn.execute('SELECT username FROM users')]
            print(f'ERROR: SEED_USERNAME "{SEED_USERNAME}" が users に存在しません。'
                  f'既存ユーザー: {names}', file=sys.stderr)
            return 1

        print(f'DB          : {DB_PATH}')
        print(f'posts 件数  : {total}')
        print(f'割り当て先  : {SEED_USERNAME}')
        print('実行内容    : posts → posts_legacy にリネーム → 新スキーマで posts を作成 → '
              '列名を明示して全件コピー → username インデックス作成')
        print('              posts_legacy は削除せず残します（ロールバック用）')

        if args.dry_run:
            print('\n[dry-run] 書き込みはしていません。')
            return 0

        # BEGIN IMMEDIATE で先に書き込みロックを取り、ロック取得後に再チェックする
        conn.isolation_level = None
        conn.execute('BEGIN IMMEDIATE')
        if 'username' in _columns(conn, 'posts'):
            conn.execute('ROLLBACK')
            print('他のプロセスが先に移行を完了しました。何もしません。')
            return 0

        conn.execute('ALTER TABLE posts RENAME TO posts_legacy')
        conn.execute(NEW_SCHEMA)
        conn.execute(
            f'INSERT INTO posts (id, post_key, username, {", ".join(COLUMNS[2:])}) '
            f'SELECT id, post_key, ?, {", ".join(COLUMNS[2:])} FROM posts_legacy',
            (SEED_USERNAME,),
        )
        conn.execute('CREATE INDEX IF NOT EXISTS idx_posts_username ON posts(username)')

        after = conn.execute('SELECT COUNT(*) FROM posts').fetchone()[0]
        legacy = conn.execute('SELECT COUNT(*) FROM posts_legacy').fetchone()[0]
        if after != total or legacy != total:
            conn.execute('ROLLBACK')
            print(f'ERROR: 件数が一致しません（移行前={total} 移行後={after} legacy={legacy}）。'
                  f'ロールバックしました。', file=sys.stderr)
            return 1

        conn.execute('COMMIT')
        print(f'\n移行完了: {after} 件（posts_legacy に {legacy} 件を保持）')
        print(f'完了時刻: {datetime.datetime.utcnow().isoformat()}Z')
        return 0
    finally:
        conn.close()


if __name__ == '__main__':
    sys.exit(main())

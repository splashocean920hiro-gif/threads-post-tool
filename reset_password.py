"""ログインアカウントのパスワードを再設定する。

パスワードは対話的に入力する（画面に表示されない）。
引数やコマンドラインに書かないので、シェル履歴にも残らない。

使い方（必ず自分のターミナルから。PTYが要るのでバックグラウンド実行では動かない）:
    cd ~/claude-test/buzz-threads-archive
    railway ssh --project <id> \
      --environment production --service buzz-threads-archive \
      -- python reset_password.py <ユーザーID>
"""

import getpass
import hashlib
import hmac
import os
import secrets
import sqlite3
import string
import sys

DB_PATH = os.environ.get('DB_PATH', os.path.join(os.path.dirname(__file__), 'data', 'threads.db'))
MIN_LEN = 8
# werkzeug の generate_password_hash(method='pbkdf2:sha256') と同じ形式・同じ反復回数。
# Railwayのコンテナでは python から werkzeug が見えない（アプリは別環境で動いている）ため、
# 標準ライブラリだけで同じハッシュを作る。形式が違うとログインできなくなるので変えないこと。
PBKDF2_ITERATIONS = 1000000
SALT_CHARS = string.ascii_letters + string.digits


def generate_password_hash(password):
    salt = ''.join(secrets.choice(SALT_CHARS) for _ in range(16))
    dk = hashlib.pbkdf2_hmac('sha256', password.encode('utf-8'),
                             salt.encode('utf-8'), PBKDF2_ITERATIONS)
    return f'pbkdf2:sha256:{PBKDF2_ITERATIONS}${salt}${dk.hex()}'


def verify_password_hash(stored, password):
    """書き込んだハッシュが本当に検証を通るか、その場で確かめるために使う。"""
    try:
        method, salt, hashval = stored.split('$', 2)
        iterations = int(method.split(':')[2])
    except (ValueError, IndexError):
        return False
    dk = hashlib.pbkdf2_hmac('sha256', password.encode('utf-8'),
                             salt.encode('utf-8'), iterations)
    return hmac.compare_digest(dk.hex(), hashval)


def main():
    if len(sys.argv) != 2:
        print('使い方: python reset_password.py <ユーザーID>', file=sys.stderr)
        return 1
    username = sys.argv[1]

    if not os.path.exists(DB_PATH):
        print(f'ERROR: DBが見つかりません: {DB_PATH}', file=sys.stderr)
        return 1

    conn = sqlite3.connect(DB_PATH, timeout=30)
    try:
        row = conn.execute('SELECT username FROM users WHERE username = ?', (username,)).fetchone()
        if not row:
            names = [r[0] for r in conn.execute('SELECT username FROM users')]
            print(f'ERROR: ユーザー "{username}" は存在しません。登録済み: {names}', file=sys.stderr)
            return 1

        print(f'ユーザー "{username}" のパスワードを再設定します。')
        try:
            pw1 = getpass.getpass('新しいパスワード（入力しても画面には出ません）: ')
            pw2 = getpass.getpass('もう一度入力してください: ')
        except EOFError:
            print('\nERROR: 対話入力ができませんでした。'
                  '自分のターミナルから実行してください（パイプ経由では動きません）。', file=sys.stderr)
            return 1

        if pw1 != pw2:
            print('ERROR: 2回の入力が一致しません。中止しました。', file=sys.stderr)
            return 1
        if len(pw1) < MIN_LEN:
            print(f'ERROR: パスワードは{MIN_LEN}文字以上にしてください。中止しました。', file=sys.stderr)
            return 1

        new_hash = generate_password_hash(pw1)
        # 書き込む前に、このハッシュで本当にログインできるか確認する。
        # 形式がずれているとログイン不能になり、復旧手段が無くなるため。
        if not verify_password_hash(new_hash, pw1):
            print('ERROR: ハッシュの自己検証に失敗しました。中止しました。', file=sys.stderr)
            return 1
        # pw_epoch を上げて既存セッションを全て無効化する。
        # 「乗っ取られたので管理者が救済する」経路なので、これが無いと
        # 攻撃者のセッションCookieが最大180日生き残る。
        # 発行済みのリセットトークンも同時に無効化する。
        conn.execute(
            'UPDATE users SET password_hash = ?, pw_epoch = COALESCE(pw_epoch,0) + 1, '
            'reset_token_hash = NULL, reset_token_expires = NULL, '
            'reset_attempts = 0, reset_locked_until = NULL '
            'WHERE username = ?',
            (new_hash, username))
        conn.commit()
        # パスワードそのものは絶対に出力しない
        print(f'\n完了しました。ユーザー "{username}" のパスワードを更新しました。')
        print('ブラウザで一度ログアウトしてから、新しいパスワードでログインしてください。')
        return 0
    finally:
        conn.close()


if __name__ == '__main__':
    sys.exit(main())

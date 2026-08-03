import hashlib
import io
import os
import re
import zipfile
import json
import time
import sqlite3
import sys
import threading
import unicodedata
import secrets
import random
import datetime
from functools import wraps

import requests
from flask import Flask, request, jsonify, send_from_directory, session, redirect
from werkzeug.middleware.proxy_fix import ProxyFix
from werkzeug.security import generate_password_hash as _gph, check_password_hash


def generate_password_hash(password):
    # scryptはOpenSSLビルド依存で環境により未対応（macOSのLibreSSL等）。pbkdf2は常に使える
    return _gph(password, method='pbkdf2:sha256')

app = Flask(__name__, static_folder='static')
# Railwayのプロキシ配下で動くため、remote_addr を実クライアントに補正する。
# X-Forwarded-For の「先頭」はクライアントが自由に詐称できるので絶対に使わない
# （先頭を使うとレート制限がランダムな偽装ヘッダで完全に無効化される）。
# ProxyFix は信頼するプロキシ段数分だけ右から数えるので、詐称値は採用されない。
# x_host は付けない。X-Forwarded-Host を信頼すると request.url_root が汚染され、
# 配布zipに埋める archiveUrl が攻撃者のドメインに書き換わりうる
# （収集データの送信先がすり替わる）。レート制限に必要なのは x_for だけ。
#
# x_for=2 の根拠（本番のアクセスログで実測）:
#   X-Forwarded-For: 126.x.x.x, 152.233.33.161
#                    ↑実クライアント  ↑Railwayのエッジ（リクエストごとに変わる）
# x_for=1 だと右端＝毎回変わるRailway側のIPを掴むため、レート制限が一切溜まらない
# （実測で30回連続アクセスしても429にならなかった）。2にして実クライアントを取る。
# クライアントが偽装値を先頭に入れても、Railwayが自分のIPを追記するので
# 右から2番目は常に実クライアントのままで、詐称できない。
app.wsgi_app = ProxyFix(app.wsgi_app, x_for=2, x_proto=1)
# SECRET_KEY が未設定だと worker ごとに違う鍵になり、
# 「ログインできるが不定期にログアウトする」という気づきにくい壊れ方をする。
_secret = os.environ.get('SECRET_KEY')
if not _secret:
    if os.environ.get('RAILWAY_ENVIRONMENT'):
        raise RuntimeError('SECRET_KEY が未設定です。設定しないとログイン状態が保持できません。')
    _secret = secrets.token_hex(32)      # ローカル開発時のみ自動生成
app.secret_key = _secret
app.permanent_session_lifetime = datetime.timedelta(days=180)
app.config['SESSION_COOKIE_SAMESITE'] = 'Lax'
# 本番(HTTPS)のみ Secure を付ける。ローカルは http なので付けると検証でセッションが保持できない
# 既定で Secure。ローカル検証のときだけ COOKIE_INSECURE=1 で明示的に外す
# （環境変数の有無で判定すると、変数が消えた時に無言で Secure が外れる）
app.config['SESSION_COOKIE_SECURE'] = os.environ.get('COOKIE_INSECURE') != '1'
app.config['SESSION_COOKIE_HTTPONLY'] = True

# 本番はRailway Volumeを/dataにマウントしてDB_PATH=/data/threads.dbを想定（再デプロイでも消えない）
DB_PATH = os.environ.get('DB_PATH', os.path.join(os.path.dirname(__file__), 'data', 'threads.db'))
INGEST_KEY = os.environ.get('INGEST_SHARED_KEY', '')
# 旧・単一パスワード時代の値。移行前からある環境の初期ユーザー作成にだけ使う（下記_seed_default_user）
APP_PASSWORD = os.environ.get('APP_PASSWORD', '')
SEED_USERNAME = os.environ.get('SEED_USERNAME', 'admin')

RETENTION_MONTHS = 12
# 既定は実API。ローカル検証でスタブに向けるためだけに環境変数で上書きできる（本番では未設定にする）
THREADS_API_BASE = os.environ.get('THREADS_API_BASE', 'https://graph.threads.net/v1.0')
# cronサービスからの実行を認証する共有キー。未設定なら /api/cron/run-due は常に503（fail-closed）
CRON_SHARED_KEY = os.environ.get('CRON_SHARED_KEY', '')
# 予約日時はブラウザのローカル時刻(JST)で作られる。夏時間が無いので固定オフセットで正しい
JST = datetime.timezone(datetime.timedelta(hours=9))
CRON_MAX_PER_RUN = 3          # 1回の実行で処理する最大件数
CRON_DEADLINE_SEC = 120       # これを超えたら新規の確保をやめて返す
CRON_CATCHUP_HOURS = 6        # これより古い未着手の予約は投稿せず failed にする
CRON_STALE_MINUTES = 15       # posting のまま放置された行を回収するまでの時間
INSIGHTS_REFRESH_DAYS = 7     # 投稿後この日数はインプレッションを更新し続ける
# 投稿文生成（Gemini呼び出し）はブラウザ側(static/tool.html)で行うため、サーバー側にモデル定義は無い

SETTINGS_KEYS = ['gemini_key', 'threads_user_id', 'threads_access_token', 'template_rules']
USERNAME_RE = re.compile(r'^[A-Za-z0-9_.-]{3,32}$')

DEFAULT_TEMPLATE_RULES = """- 各投稿は「本文」+「リプライ1」+「リプライ2」で構成されるツリー投稿にする
- 本文は120〜200字程度、リプライは80〜150字程度
- 経営者目線で、フォロワーに刺さる具体的な内容にする（抽象的な精神論だけにしない）
- 参照投稿の切り口・口調・テーマ傾向を参考にするが、丸写しはしない
- 絵文字は使っても1〜2個までにする"""


_volume_warned = [False]


def _warn_if_not_persistent():
    """Volumeが付いていないと、再デプロイのたびにDBごと消える。
    しかも次のデプロイまでは完全に正常動作するので、購入者は気づけない。
    起動時に一度だけ大きく警告する。"""
    if _volume_warned[0] or not os.environ.get('RAILWAY_ENVIRONMENT'):
        return
    _volume_warned[0] = True
    mount = os.environ.get('RAILWAY_VOLUME_MOUNT_PATH')
    if not mount or not os.path.abspath(DB_PATH).startswith(os.path.abspath(mount)):
        sys.stderr.write(
            '\n' + '!' * 70 + '\n'
            '警告: データベースが永続ストレージ(Volume)の外に置かれています。\n'
            f'  DB_PATH = {DB_PATH}\n'
            f'  Volume  = {mount or "（マウントされていません）"}\n'
            'このままだと、次に再デプロイした時点で登録内容・予約投稿・収集した投稿が\n'
            'すべて消えます。RailwayでVolumeを /data にマウントし、\n'
            'DB_PATH=/data/threads.db を設定してください。\n'
            + '!' * 70 + '\n\n')
        sys.stderr.flush()


def _get_db():
    _warn_if_not_persistent()
    os.makedirs(os.path.dirname(DB_PATH), exist_ok=True)
    conn = sqlite3.connect(DB_PATH, timeout=30)
    conn.row_factory = sqlite3.Row
    return conn


def _column_exists(conn, table, column):
    cols = [r['name'] for r in conn.execute(f'PRAGMA table_info({table})').fetchall()]
    return column in cols


def _ensure_username_column(conn, table):
    """既存テーブルにusernameカラムを後付けする（マイグレーション）。
    既存行はSEED_USERNAMEに割り当てる（統合前に入っていたデータはユーザー本人のものとみなす）。"""
    if not _column_exists(conn, table, 'username'):
        conn.execute(f'ALTER TABLE {table} ADD COLUMN username TEXT')
        conn.execute(f'UPDATE {table} SET username = ? WHERE username IS NULL', (SEED_USERNAME,))


def _init_db():
    conn = _get_db()
    # ユーザーアカウント（ID＋bcryptパスワード）
    conn.execute('''
        CREATE TABLE IF NOT EXISTS users (
            username TEXT PRIMARY KEY,
            password_hash TEXT NOT NULL,
            created_at TEXT NOT NULL
        )
    ''')
    # buzz-threads-collector拡張機能が収集した投稿（usernameでユーザーごとに分離）
    conn.execute('''
        CREATE TABLE IF NOT EXISTS posts (
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
            UNIQUE(post_key, username)
        )
    ''')
    # 手動登録された参照投稿（ユーザーごと）
    conn.execute('''
        CREATE TABLE IF NOT EXISTS manual_references (
            id TEXT PRIMARY KEY,
            username TEXT,
            main TEXT,
            reply1 TEXT,
            reply2 TEXT,
            url TEXT,
            date TEXT,
            likes INTEGER,
            replies INTEGER,
            impressions INTEGER,
            created_at TEXT NOT NULL
        )
    ''')
    # 投稿スケジュール（ユーザーごと）
    conn.execute('''
        CREATE TABLE IF NOT EXISTS schedule_posts (
            id TEXT PRIMARY KEY,
            username TEXT,
            date TEXT NOT NULL,
            time TEXT NOT NULL,
            main TEXT,
            reply1 TEXT,
            reply2 TEXT,
            status TEXT NOT NULL DEFAULT 'draft',
            impressions INTEGER,
            error TEXT,
            created_at TEXT NOT NULL,
            updated_at TEXT NOT NULL
        )
    ''')
    # 生成済み・未予約のドラフト（ユーザーごと）
    conn.execute('''
        CREATE TABLE IF NOT EXISTS drafts (
            id TEXT PRIMARY KEY,
            username TEXT,
            date TEXT,
            time TEXT,
            main TEXT,
            reply1 TEXT,
            reply2 TEXT,
            created_at TEXT NOT NULL
        )
    ''')
    # APIキー・投稿テンプレート等の設定（ユーザー×キー）
    conn.execute('''
        CREATE TABLE IF NOT EXISTS user_settings (
            username TEXT NOT NULL,
            key TEXT NOT NULL,
            value TEXT,
            PRIMARY KEY (username, key)
        )
    ''')
    conn.commit()

    # 既存テーブルへのusernameカラム後付け（統合前デプロイからのマイグレーション）
    for t in ('manual_references', 'schedule_posts', 'drafts'):
        _ensure_username_column(conn, t)
    # postsテーブルに「本人の連投(ツリー)」を保存するカラムを後付け（JSON配列）
    if not _column_exists(conn, 'posts', 'replies_json'):
        conn.execute('ALTER TABLE posts ADD COLUMN replies_json TEXT')
    # posts の username 分離は migrate_posts.py（ワンショット）で行う。
    # ここでインデックスを無条件に作ると、未移行のDBでは `no such column: username` で
    # 起動時例外になり、worker が再起動ループに入るため必ず存在確認する。
    if _column_exists(conn, 'posts', 'username'):
        conn.execute('CREATE INDEX IF NOT EXISTS idx_posts_username ON posts(username)')
    # Threadsアカウント（1ログインアカウントの中に複数持てる）
    conn.execute('''
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
    ''')
    conn.execute('CREATE INDEX IF NOT EXISTS idx_threads_accounts_username '
                 'ON threads_accounts(username)')
    # account_id の後付け。既存データの割り当ては migrate_accounts.py（ワンショット）で行うが、
    # 列とテーブルの作成はここでやらないと新規環境で永久に作られず全APIが500になる。
    for _t in ('schedule_posts', 'drafts', 'manual_references', 'posts'):
        try:
            if not _column_exists(conn, _t, 'account_id'):
                conn.execute(f'ALTER TABLE {_t} ADD COLUMN account_id TEXT')
        except sqlite3.OperationalError:
            pass
        try:
            conn.execute(f'CREATE INDEX IF NOT EXISTS idx_{_t}_account ON {_t}(account_id)')
        except sqlite3.OperationalError:
            pass
    # パスワード再設定（秘密の質問）用。すべて nullable なのでデータ移行は不要。
    # 答えは平文で持たず、ハッシュだけを保存する。
    # パスワード変更時に既存セッションを失効させるための世代番号。
    # これが無いと「乗っ取られたからパスワードを変える」が機能しない
    # （攻撃者のセッションCookieは180日生き続ける）。
    try:
        if not _column_exists(conn, 'users', 'pw_epoch'):
            conn.execute('ALTER TABLE users ADD COLUMN pw_epoch INTEGER NOT NULL DEFAULT 0')
    except sqlite3.OperationalError:
        pass
    for _c in ('email', 'secret_question', 'secret_answer_hash',
               'reset_token_hash', 'reset_token_expires', 'reset_locked_until'):
        try:
            if not _column_exists(conn, 'users', _c):
                conn.execute(f'ALTER TABLE users ADD COLUMN {_c} TEXT')
        except sqlite3.OperationalError:
            pass
    try:
        if not _column_exists(conn, 'users', 'reset_attempts'):
            conn.execute('ALTER TABLE users ADD COLUMN reset_attempts INTEGER NOT NULL DEFAULT 0')
    except sqlite3.OperationalError:
        pass
    # 拡張機能に配るユーザー個別のトークン。worker が2本あるので競合を握って再確認する
    try:
        if not _column_exists(conn, 'users', 'ingest_token'):
            conn.execute('ALTER TABLE users ADD COLUMN ingest_token TEXT')
    except sqlite3.OperationalError:
        pass
    try:
        conn.execute('CREATE UNIQUE INDEX IF NOT EXISTS idx_users_ingest_token '
                     'ON users(ingest_token)')
    except sqlite3.OperationalError:
        pass
    # schedule_postsに投稿進捗(本文ID・リプライID)を保存するカラムを後付け（再実行時の重複防止）
    if not _column_exists(conn, 'schedule_posts', 'thread_state'):
        conn.execute('ALTER TABLE schedule_posts ADD COLUMN thread_state TEXT')
    conn.commit()
    conn.close()


def _seed_default_user():
    """初期ユーザーを作成する。パスワードはAPP_PASSWORD（旧・単一パスワード）を流用。
    既に存在する場合は何もしない（ユーザーが新規登録し直しても上書きしない）。"""
    if not APP_PASSWORD:
        return
    conn = _get_db()
    row = conn.execute('SELECT username FROM users WHERE username = ?', (SEED_USERNAME,)).fetchone()
    if not row:
        conn.execute(
            'INSERT INTO users (username, password_hash, created_at) VALUES (?, ?, ?)',
            (SEED_USERNAME, generate_password_hash(APP_PASSWORD), datetime.datetime.utcnow().isoformat()),
        )
        conn.commit()
    conn.close()


_init_db()
_seed_default_user()


def _post_key(url):
    m = re.search(r'/post/([A-Za-z0-9_-]+)', url or '')
    return m.group(1) if m else None


# 収集した本人連投(ツリー)のノイズ除去:
#  - 行頭の絶対日付(2026/04/23 等)を削る（Threadsが古い投稿にタイムスタンプを日付で出すため混入する）
#  - 本文と重複する連投を除く（拡張機能側で本文がリプ先頭に重複することがある）
_DATE_PREFIX_RE = re.compile(r'^\s*\d{4}[/／年.\-]\s*\d{1,2}[/／月.\-]\s*\d{1,2}日?\s*')
def _clean_reply_texts(main, replies):
    main_norm = re.sub(r'\s+', '', main or '')[:40]
    out = []
    for r in (replies or []):
        t = _DATE_PREFIX_RE.sub('', str(r or '')).strip()
        if not t:
            continue
        if re.sub(r'\s+', '', t)[:40] == main_norm:  # 本文の重複
            continue
        out.append(t)
    return out


def _cleanup_old(conn):
    """12ヶ月経った投稿は削除する（古い投稿を参考にさせないため）。"""
    conn.execute(
        f"DELETE FROM posts WHERE first_seen_at < datetime('now', '-{RETENTION_MONTHS} months')"
    )


# CORS許可は撤去した。/api/settings が gemini_key と threads_access_token を平文で返すため、
# 購入者のシークレットを預かる状態で外部オリジンからの credentialed アクセスを許すのは危険。
# 唯一の利用者だった旧GitHub Pages版のデモページは、/api/reference-posts のログイン必須化で
# どのみち同期が動かなくなるため、許可を残す理由がない。
@app.after_request
def _add_security_headers(resp):
    resp.headers['X-Content-Type-Options'] = 'nosniff'
    return resp


# ---------------------------------------------------------------------------
# 認証
#   - X-Ingest-Key ヘッダ: buzz-threads-collector拡張機能 → /ingest 専用（ユーザーごとに発行）
#   - セッションCookie（ユーザーID＋パスワードでログイン）: Threads運用ツール本体 → /api/* 用
# ---------------------------------------------------------------------------

def _ingest_username():
    """X-Ingest-Key ヘッダから、書き込み先のユーザー名を解決する。
    解決できなければ None（＝拒否）。空文字同士が一致して素通りしないよう、
    ヘッダが空の時点で先に落とし、旧共通キーの分岐にも bool() ガードを掛ける。"""
    token = request.headers.get('X-Ingest-Key', '')
    if not token:
        return None
    conn = _get_db()
    try:
        row = conn.execute(
            'SELECT username FROM users WHERE ingest_token = ?', (token,)
        ).fetchone()
    finally:
        conn.close()
    if row:
        return row['username']
    # 後方互換: 旧・全員共通キー。移行前から動いている拡張機能を止めないために残す。
    # 新規環境（テンプレートからのデプロイ）では INGEST_SHARED_KEY を設定しないので、
    # この分岐は発火しない。移行が済んだら環境変数ごと削除してよい。
    if INGEST_KEY and secrets.compare_digest(token.encode('utf-8', 'ignore'),
                                             INGEST_KEY.encode('utf-8', 'ignore')):
        # 着地先が実在しないと、誰からも見えない投稿が溜まり続ける。
        # （SEED_USERNAME のユーザーが作られていない環境で起こりうる）
        conn = _get_db()
        try:
            exists = conn.execute('SELECT 1 FROM users WHERE username = ?',
                                  (SEED_USERNAME,)).fetchone()
        finally:
            conn.close()
        return SEED_USERNAME if exists else None
    return None


def _issue_ingest_token(conn, username):
    """ユーザーのIngestトークンを返す。未発行なら発行して保存する。"""
    row = conn.execute('SELECT ingest_token FROM users WHERE username = ?', (username,)).fetchone()
    if row and row['ingest_token']:
        return row['ingest_token']
    token = secrets.token_urlsafe(32)
    conn.execute('UPDATE users SET ingest_token = ? WHERE username = ?', (token, username))
    conn.commit()
    return token


def current_username():
    """ログイン中のユーザー名。パスワードが変更されていたらセッションを無効にする。

    Cookieの寿命は180日なので、これが無いと「乗っ取られたからパスワードを変えた」
    あとも攻撃者のセッションが生き続ける。パスワード変更のたびに pw_epoch を上げ、
    ログイン時にセッションへ焼いた値と突き合わせる。"""
    u = session.get('username')
    if not u:
        return None
    conn = _get_db()
    try:
        r = conn.execute('SELECT pw_epoch FROM users WHERE username = ?', (u,)).fetchone()
    except sqlite3.OperationalError:
        return u          # 列が未作成の起動直後は従来どおり通す
    finally:
        conn.close()
    if not r:
        session.clear()
        return None
    if session.get('pw_epoch', 0) != (r['pw_epoch'] or 0):
        session.clear()
        return None
    return u


def api_login_required(f):
    @wraps(f)
    def wrapper(*args, **kwargs):
        if request.method == 'OPTIONS':
            return '', 204
        if not current_username():
            return jsonify({'error': 'unauthorized'}), 401
        return f(*args, **kwargs)
    return wrapper


def page_login_required(f):
    @wraps(f)
    def wrapper(*args, **kwargs):
        if not current_username():
            return redirect('/login')
        return f(*args, **kwargs)
    return wrapper


@app.route('/api/register', methods=['POST', 'OPTIONS'])
def register():
    if request.method == 'OPTIONS':
        return '', 204
    # pbkdf2(100万回) を未認証で回せるので、ハッシュ計算より前に落とす
    if not _rate_ok('register', REGISTER_IP_LIMIT, REGISTER_IP_WINDOW_SEC):
        return jsonify({'error': '登録の試行が多すぎます。しばらく待ってからやり直してください'}), 429
    payload = request.get_json(silent=True) or {}
    username = (payload.get('username') or '').strip()
    password = payload.get('password') or ''
    if not USERNAME_RE.match(username):
        return jsonify({'error': 'IDは英数字・_.- の3〜32文字で入力してください'}), 400
    if len(password) < 8:
        return jsonify({'error': 'パスワードは8文字以上にしてください'}), 400
    conn = _get_db()
    # 自分専用の環境なので、最初の1人だけ自由に登録できればよい。
    # 開けたままだと、URLを知った第三者に登録されて課金とAPI実行枠を使われる。
    # 2人目以降を許すときは REGISTER_CODE を設定する。
    n_users = conn.execute('SELECT COUNT(*) FROM users').fetchone()[0]
    if n_users > 0:
        code = os.environ.get('REGISTER_CODE', '')
        given = (payload.get('register_code') or '').strip()
        if not code or not given or not secrets.compare_digest(
                given.encode('utf-8', 'ignore'), code.encode('utf-8', 'ignore')):
            conn.close()
            return jsonify({'error': 'このツールでは新規登録は受け付けていません'}), 403
    exists = conn.execute('SELECT username FROM users WHERE username = ?', (username,)).fetchone()
    if exists:
        conn.close()
        return jsonify({'error': 'このIDは既に使われています'}), 409
    conn.execute(
        'INSERT INTO users (username, password_hash, created_at) VALUES (?, ?, ?)',
        (username, generate_password_hash(password), datetime.datetime.utcnow().isoformat()),
    )
    conn.commit()
    conn.close()
    session.permanent = True
    session['username'] = username
    session['pw_epoch'] = 0        # 新規作成なので必ず0
    return jsonify({'ok': True, 'username': username})


@app.route('/api/login', methods=['POST', 'OPTIONS'])
def login():
    if request.method == 'OPTIONS':
        return '', 204
    # check_password_hash は pbkdf2(100万回)=約0.2秒。未認証で無制限に回せると
    # アプリ全体（cronの予約投稿を含む）を止められるので、ハッシュ計算より前に落とす。
    # アカウント単位のロックにはしない（IDを知る第三者が正規利用者を締め出せるため）。
    if not _rate_ok('login', LOGIN_IP_LIMIT, LOGIN_IP_WINDOW_SEC):
        return jsonify({'error': 'ログインの試行が多すぎます。しばらく待ってからやり直してください'}), 429
    payload = request.get_json(silent=True) or {}
    username = (payload.get('username') or '').strip()
    password = payload.get('password') or ''
    conn = _get_db()
    row = conn.execute('SELECT * FROM users WHERE username = ?', (username,)).fetchone()
    conn.close()
    if not row or not check_password_hash(row['password_hash'], password):
        return jsonify({'error': 'IDまたはパスワードが違います'}), 401
    session.permanent = True
    session['username'] = username
    session['pw_epoch'] = (row['pw_epoch'] if 'pw_epoch' in row.keys() else 0) or 0
    return jsonify({'ok': True, 'username': username})


@app.route('/api/logout', methods=['POST'])
def logout():
    session.clear()
    return jsonify({'ok': True})


@app.route('/api/session')
def session_status():
    return jsonify({'authed': bool(current_username()), 'username': current_username()})


@app.route('/api/threads/resolve-user-id', methods=['POST', 'OPTIONS'])
@api_login_required
def resolve_threads_user_id():
    """アクセストークンからThreadsのUser IDを自動取得する（graph.threads.net/me）。
    graph.threads.netはブラウザから直接呼べない(CORS)ため、サーバー側で叩く。"""
    if request.method == 'OPTIONS':
        return '', 204
    payload = request.get_json(silent=True) or {}
    token = (payload.get('access_token') or '').strip()
    if not token:
        return jsonify({'error': 'access_token required'}), 400
    try:
        r = requests.get(f'{THREADS_API_BASE}/me', params={'fields': 'id,username'},
                         headers={'Authorization': f'Bearer {token}'}, timeout=15)
        if not r.ok:
            return jsonify({'error': f'{r.status_code} {_redact(r.text)[:200]}'}), 400
        d = r.json()
        if not d.get('id'):
            return jsonify({'error': 'User IDが取得できませんでした（トークンを確認してください）'}), 400
        return jsonify({'user_id': d.get('id', ''), 'username': d.get('username', '')})
    except Exception as e:
        return jsonify({'error': _redact(e)[:200]}), 500


# ---------------------------------------------------------------------------
# 収集（buzz-threads-collector拡張機能）※全ユーザー共有の公開バズ投稿プール、変更なし
# ---------------------------------------------------------------------------

# ---------------------------------------------------------------------------
# Threadsアカウント（1ログインアカウントの中に複数持てる）
#   予約投稿・下書き・参照投稿・収集素材は account_id で分ける。
#   収集素材の振り分けは keyword で行う（拡張機能は1トークンしか持てないため、
#   アカウントごとにトークンを配ると運用が成立しない）。
# ---------------------------------------------------------------------------

def _normalize_keyword(kw):
    """振り分け用にキーワードを正規化する。
    実データに「AI　副業」(全角スペース)と「AI 副業」(半角)が混在していたため、
    NFKC で幅を揃えたうえで内部の連続空白も畳む。前後空白・先頭の#・大小文字も吸収する。"""
    t = unicodedata.normalize('NFKC', str(kw or ''))
    t = t.strip().lstrip('#').strip().lower()
    return re.sub(r'\s+', ' ', t)


def _user_accounts(conn, user):
    return conn.execute(
        'SELECT * FROM threads_accounts WHERE username = ? ORDER BY sort_order, created_at',
        (user,),
    ).fetchall()


def _resolve_account(conn, user, account_id):
    """リクエストで指定された account_id が本人のものか検証して返す。
    他人のIDを渡されても絶対に通さない。未指定なら先頭のアカウントを既定にする。"""
    accounts = _user_accounts(conn, user)
    if account_id:
        for a in accounts:
            if a['id'] == account_id:
                return a
        return None
    return accounts[0] if accounts else None


def _account_id_for_keyword(conn, user, keyword):
    """収集した投稿を、担当キーワードからどのアカウントの素材か判定する。
    一致しなければ None（未分類）。黙って捨てず、画面から手動で割り当てられる。"""
    norm = _normalize_keyword(keyword)
    if not norm:
        return None
    for a in _user_accounts(conn, user):
        for k in (a['keywords'] or '').replace(',', '\n').split('\n'):
            if _normalize_keyword(k) == norm:
                return a['id']
    return None


def _account_credentials(conn, row):
    """予約投稿の行から、投稿に使うThreadsの認証情報を取り出す。
    account_id が無い行（移行前に作られたもの）は user_settings を見る（後方互換）。"""
    acc_id = row['account_id'] if 'account_id' in row.keys() else None
    if acc_id:
        a = conn.execute(
            'SELECT threads_user_id, threads_access_token FROM threads_accounts '
            'WHERE id = ? AND username = ?', (acc_id, row['username']),
        ).fetchone()
        if a:
            return (a['threads_user_id'] or '').strip(), (a['threads_access_token'] or '').strip()
    # アカウントを1件でも持つユーザーの account_id=NULL 行に旧トークンを当てると、
    # 「削除したアカウントの予約が別アカウントで公開される」事故になる。
    # フォールバックはアカウントを1件も持たないユーザー（移行前の状態）に限る。
    if conn.execute('SELECT 1 FROM threads_accounts WHERE username = ?',
                    (row['username'],)).fetchone():
        return '', ''
    settings = _get_settings_dict(conn, row['username'])
    return ((settings.get('threads_user_id') or '').strip(),
            (settings.get('threads_access_token') or '').strip())


def _account_public(a, include_secrets=False):
    """一覧APIで返す形。トークンは既定で返さない（設定済みかどうかだけ返す）。"""
    d = {
        'id': a['id'], 'label': a['label'], 'keywords': a['keywords'] or '',
        'threads_user_id': a['threads_user_id'] or '',
        'has_token': bool((a['threads_access_token'] or '').strip()),
        'sort_order': a['sort_order'],
    }
    if include_secrets:
        d['threads_access_token'] = a['threads_access_token'] or ''
    return d


def _req_account(conn, user):
    """?account=<id> を解決する。他人のIDや存在しないIDは None を返し、
    呼び出し側は 404 を返す（存在の有無を漏らさないため 403 ではなく 404 に統一）。"""
    raw = request.args.get('account')
    if raw is not None and not raw.strip():
        return None       # 空白だけの指定は「不正なID」として扱い、先頭に落とさない
    return _resolve_account(conn, user, (raw or '').strip() or None)


# ---------------------------------------------------------------------------
# パスワード再設定（秘密の質問）
#   平文パスワードは復元できない（pbkdf2の一方向ハッシュ）ので、
#   「通知」ではなく「本人確認のうえ再設定させる」形にしている。
#   質問文は利用者が自由入力する。固定の選択肢（母親の旧姓など）は
#   推測・検索されやすく、認証手段として弱いため採らない。
# ---------------------------------------------------------------------------

RESET_MAX_ATTEMPTS = 5          # これだけ失敗したらロックする
RESET_LOCK_MINUTES = 15
RESET_TOKEN_MINUTES = 30
SECRET_Q_MAX = 200
SECRET_A_MIN = 8            # 短すぎる答えは総当たりされる
SECRET_A_MAX = 200
EMAIL_MAX = 254
# ユーザーID総当たりを抑える。プロセス内メモリなのでworkerを跨がないが、
# 秘密の質問側のロック（DB）と併用するので実効性はある。
_rate_hits = {}
_rate_lock = threading.Lock()
# worker2本 × スレッド4なのでプロセスを跨ぐと実効上限が増える。
# それを見越して低めに設定し、DB側のロック（秘密の質問の失敗回数）と併用する。
FORGOT_IP_LIMIT = 10
FORGOT_IP_WINDOW_SEC = 600
# ログイン・新規登録も未認証で pbkdf2(100万回・1回0.2秒) を回せるため、
# 制限が無いとアプリ全体（cronの予約投稿を含む）を止められる。
# ただし「そのユーザーをロックする」形にはしない。IDを知る第三者が
# 失敗を繰り返すだけで正規利用者を締め出せてしまうため、IP単位に留める。
LOGIN_IP_LIMIT = 30
LOGIN_IP_WINDOW_SEC = 300
REGISTER_IP_LIMIT = 10
REGISTER_IP_WINDOW_SEC = 3600
# レート制限テーブルの上限。実測で 2万キー×30タイムスタンプ = 約23.6MB/worker
# （--workers 2 なので最悪47MB程度）。これを下げると、大量のIPで辞書を溢れさせて
# 他人のカウンタを流せてしまうため、あえて到達しにくい水準に置いている。
RATE_MAX_KEYS = 20000


def _normalize_answer(a):
    """秘密の質問の答えを照合用に正規化する。
    全角半角・大文字小文字の差を吸収し、空白は「全て除去」する。

    日本語は空白の入れ方が人によって揺れるので、内部空白を1つに畳むだけだと
    「とりあえずやってみよう」で登録した人が「とりあえず やってみよう」と
    打った時点で復旧できなくなる。答えは8文字以上を必須にしているので、
    空白を落としてもエントロピーは実質的に減らない。"""
    t = unicodedata.normalize('NFKC', str(a or '')).lower()
    return re.sub(r'\s+', '', t)


def _hash_answer(value):
    """秘密の質問の答え用。人間が決める低エントロピーな値なので伸長が要る。"""
    return generate_password_hash(value)


def _hash_token(token):
    """リセットトークン用。secrets.token_urlsafe(32) = 256bit の乱数なので
    鍵伸長は不要。pbkdf2(100万回)を未認証経路で回すとCPU枯渇の的になる。"""
    return hashlib.sha256(token.encode('utf-8')).hexdigest()


def _client_ip():
    """ProxyFix が補正済みの remote_addr を使う。
    X-Forwarded-For を自前で読んで先頭を採るのは禁止（詐称でレート制限が無効化される）。"""
    return request.remote_addr or 'unknown'


def _rate_ok(bucket, limit, window_sec):
    """IP単位のレート制限。bucket でエンドポイント種別を分ける。

    read-modify-write なのでロックが要る（4スレッドでカウント落ちするため）。
    プロセス内メモリなので worker 数だけ実効上限が増える点は許容している
    （正確さより、未認証で重い処理を無限に回せない状態を作ることが目的）。"""
    now = time.time()
    key = (bucket, _client_ip())
    with _rate_lock:
        _, prev = _rate_hits.get(key, (window_sec, []))
        hits = [t for t in prev if now - t < window_sec]
        if len(hits) >= limit:
            _rate_hits[key] = (window_sec, hits)
            return False
        hits.append(now)
        # 各エントリに自分の window を持たせる。呼び出し元の window で
        # 辞書全体を掃除すると、長い window の bucket（register=3600秒）が
        # 短い window の掃除（login=300秒）で早期にリセットされてしまう。
        _rate_hits[key] = (window_sec, hits)
        if len(_rate_hits) > RATE_MAX_KEYS:
            # 期限切れのキーだけを消す
            for k, (w, v) in list(_rate_hits.items()):
                if not any(now - t < w for t in v):
                    _rate_hits.pop(k, None)
            # 全キーが有効な分散フラッドでは1件も消えないのでメモリ上限を持たせる。
            # ここで落とすと相手のカウンタもリセットされてしまうため、上限は
            # 「そこまでIPを用意できるならIP単位制限自体が無意味」な水準に置く。
            if len(_rate_hits) > RATE_MAX_KEYS:
                for k, _v in sorted(_rate_hits.items(),
                                    key=lambda kv: max(kv[1][1]))[:RATE_MAX_KEYS // 4]:
                    _rate_hits.pop(k, None)
    return True


def _forgot_rate_ok():
    """復旧系のIP単位レート制限。question と verify に適用する。

    reset には掛けていない。トークン照合の前に走るのは sha256 とDB読み取りだけで
    pbkdf2（1回0.2秒）は照合を通過した後にしか実行されず、トークンは256bitの
    乱数なので総当たりも成立しないため。"""
    return _rate_ok('forgot', FORGOT_IP_LIMIT, FORGOT_IP_WINDOW_SEC)


def _reset_locked(row):
    """復旧フロー専用のロック判定。
    【不変条件】このロックを /api/login の判定に使ってはいけない。
    使うと、IDを知っている第三者が失敗を繰り返すだけで正規利用者を
    通常ログインから締め出せる（ロックを悪用したDoS）。"""
    until = row['reset_locked_until'] if 'reset_locked_until' in row.keys() else None
    if not until:
        return False
    return datetime.datetime.utcnow().isoformat() < until


def _mask_email(addr):
    if not addr or '@' not in addr:
        return ''
    name, dom = addr.split('@', 1)
    return (name[0] + '***' if name else '***') + '@' + dom


def _send_reset_mail(to_addr, token, username):
    """メール送信の差し込み口。プロバイダ未設定なら何もせず False を返す。
    False の場合、呼び出し側はトークンを画面に表示する（秘密の質問に
    正解した本人だけが到達できる画面なので、そこで渡して問題ない）。"""
    if not to_addr:
        return False
    provider = os.environ.get('MAIL_PROVIDER')
    if not provider:
        return False
    # MAIL_PROVIDER が設定されているのに送信処理が無い状態で False を返すと、
    # 「メールを送ったつもりでトークンが画面に出続ける」無言の誤設定になる。
    # 実装するまでは明示的に落とす。
    raise RuntimeError('MAIL_PROVIDER が設定されていますが、メール送信は未実装です。'
                       '実装するか、環境変数を外してください。')


@app.route('/api/recovery', methods=['GET', 'PUT'])
@api_login_required
def api_recovery():
    user = current_username()
    conn = _get_db()
    try:
        if request.method == 'GET':
            r = conn.execute('SELECT email, secret_question, secret_answer_hash '
                             'FROM users WHERE username = ?', (user,)).fetchone()
            # 答えは絶対に返さない。設定済みかどうかだけ伝える
            return jsonify({
                'email_masked': _mask_email(r['email'] if r else ''),
                'has_email': bool(r and r['email']),
                'secret_question': (r['secret_question'] or '') if r else '',
                'has_secret': bool(r and r['secret_question'] and r['secret_answer_hash']),
            })

        payload = request.get_json(silent=True) or {}
        # 秘密の質問は「パスワードと同等の認証手段」になる。セッションを一時的に
        # 握られただけで質問と答えを差し替えられると、恒久的なバックドアになるため、
        # 変更には現在のパスワードを要求する。
        cur = conn.execute('SELECT password_hash FROM users WHERE username = ?', (user,)).fetchone()
        if not cur or not check_password_hash(cur['password_hash'],
                                              payload.get('current_password') or ''):
            return jsonify({'error': '現在のパスワードが正しくありません'}), 403
        fields, values = [], []
        if 'email' in payload:
            email = (payload.get('email') or '').strip()[:EMAIL_MAX]
            if email and not re.match(r'^[^@\s]+@[^@\s]+\.[^@\s]+$', email):
                return jsonify({'error': 'メールアドレスの形式が正しくありません'}), 400
            fields.append('email = ?'); values.append(email)
        if 'secret_question' in payload or 'secret_answer' in payload:
            q = (payload.get('secret_question') or '').strip()[:SECRET_Q_MAX]
            a = (payload.get('secret_answer') or '').strip()[:SECRET_A_MAX]
            if q and a:
                # 「犬」「はい」のような短い答えを許すと候補が数千通りしかなく、
                # ロックを掛けても数日で総当たりされる。単語ではなく短い文を求める。
                if len(_normalize_answer(a)) < SECRET_A_MIN:
                    return jsonify({'error': f'答えは{SECRET_A_MIN}文字以上にしてください。'
                                             f'単語ではなく短い文にすると推測されにくくなります'}), 400
                fields.append('secret_question = ?'); values.append(q)
                fields.append('secret_answer_hash = ?'); values.append(_hash_answer(_normalize_answer(a)))
            elif q and not a and 'secret_answer' not in payload:
                # 答えを送っていない＝既存の答えを維持したまま質問文だけ直したい場合。
                # 画面の「設定済みの場合は空欄のまま」という案内と挙動を一致させる。
                cur_a = conn.execute('SELECT secret_answer_hash FROM users WHERE username = ?',
                                     (user,)).fetchone()
                if not (cur_a and cur_a['secret_answer_hash']):
                    return jsonify({'error': '初回は質問と答えの両方を入力してください'}), 400
                fields.append('secret_question = ?'); values.append(q)
            elif not q and not a:
                # 両方空なら未設定に戻す（片方だけだと復旧に使えないため）
                fields.append('secret_question = ?'); values.append(None)
                fields.append('secret_answer_hash = ?'); values.append(None)
            else:
                return jsonify({'error': '質問と答えは両方入力してください'}), 400
        if fields:
            # 「答えを知られたかもしれないので変える」というインシデント対応で
            # 発行済みトークンが30分間生き残ると意味が無い。同時に失効させる。
            fields.append('reset_token_hash = ?'); values.append(None)
            fields.append('reset_token_expires = ?'); values.append(None)
            values.append(user)
            conn.execute(f'UPDATE users SET {", ".join(fields)} WHERE username = ?', values)
            conn.commit()
        r = conn.execute('SELECT email, secret_question, secret_answer_hash '
                         'FROM users WHERE username = ?', (user,)).fetchone()
        return jsonify({
            'email_masked': _mask_email(r['email'] or ''),
            'has_email': bool(r['email']),
            'secret_question': r['secret_question'] or '',
            'has_secret': bool(r['secret_question'] and r['secret_answer_hash']),
        })
    finally:
        conn.close()


@app.route('/api/forgot/question', methods=['POST'])
def api_forgot_question():
    """ユーザーIDから秘密の質問を返す。

    質問文を出す時点でユーザーIDの存在は隠せない（ダミーの質問を返すと、
    正規の利用者が「自分の質問と違う」と混乱して復旧できなくなる）。
    そのため存在の秘匿は諦め、代わりにIP単位のレート制限で総当たりを抑える。"""
    if not _forgot_rate_ok():
        return jsonify({'error': '試行が多すぎます。しばらく待ってからやり直してください'}), 429
    payload = request.get_json(silent=True) or {}
    username = (payload.get('username') or '').strip()
    if not username:
        return jsonify({'error': 'ユーザーIDを入力してください'}), 400
    conn = _get_db()
    try:
        r = conn.execute('SELECT secret_question, secret_answer_hash, reset_locked_until '
                         'FROM users WHERE username = ?', (username,)).fetchone()
        if not r:
            return jsonify({'error': 'このユーザーIDは登録されていません'}), 404
        if not (r['secret_question'] and r['secret_answer_hash']):
            return jsonify({'error': 'このアカウントには秘密の質問が設定されていないため、'
                                     'この方法では再設定できません。管理者に連絡してください'}), 409
        if _reset_locked(r):
            return jsonify({'error': '試行回数の上限を超えたため一時的にロックされています。'
                                     'しばらく待ってからやり直してください'}), 429
        return jsonify({'question': r['secret_question']})
    finally:
        conn.close()


@app.route('/api/forgot/verify', methods=['POST'])
def api_forgot_verify():
    """秘密の質問の答えを検証し、正しければワンタイムトークンを発行する。"""
    if not _forgot_rate_ok():
        return jsonify({'error': '試行が多すぎます。しばらく待ってからやり直してください'}), 429
    payload = request.get_json(silent=True) or {}
    username = (payload.get('username') or '').strip()
    answer = payload.get('answer') or ''
    conn = _get_db()
    try:
        r = conn.execute('SELECT * FROM users WHERE username = ?', (username,)).fetchone()
        if not r or not (r['secret_question'] and r['secret_answer_hash']):
            return jsonify({'error': '再設定できませんでした'}), 400
        # ロック判定は必ずハッシュ計算より前に行う。
        # 答えの照合は pbkdf2 100万回で1回0.2秒かかるので、未認証のまま何度も
        # 走らせられるとアプリ全体（cronの予約投稿を含む）を止められる。
        if _reset_locked(r):
            return jsonify({'error': '試行回数の上限を超えたため一時的にロックされています'}), 429
        # ロックが明けていたら失敗回数を0に戻す。
        # 戻さないと、一度5回間違えた人はその後1回失敗するたびにロックされ続ける。
        if r['reset_locked_until'] and not _reset_locked(r):
            conn.execute('UPDATE users SET reset_attempts = 0, reset_locked_until = NULL '
                         'WHERE username = ?', (username,))
            conn.commit()
            r = conn.execute('SELECT * FROM users WHERE username = ?', (username,)).fetchone()

        if not check_password_hash(r['secret_answer_hash'], _normalize_answer(answer)):
            # 加算は SQL 1文で行う（worker2本×4スレッドで読み書きが競合するため）
            conn.execute('UPDATE users SET reset_attempts = COALESCE(reset_attempts,0) + 1 '
                         'WHERE username = ?', (username,))
            conn.commit()
            attempts = conn.execute('SELECT reset_attempts FROM users WHERE username = ?',
                                    (username,)).fetchone()['reset_attempts'] or 0
            locked = None
            if attempts >= RESET_MAX_ATTEMPTS:
                locked = (datetime.datetime.utcnow()
                          + datetime.timedelta(minutes=RESET_LOCK_MINUTES)).isoformat()
                conn.execute('UPDATE users SET reset_locked_until = ?, reset_attempts = 0 '
                             'WHERE username = ?', (locked, username))
                conn.commit()
            remain = max(0, RESET_MAX_ATTEMPTS - attempts)
            return jsonify({'error': '答えが違います'
                                     + (f'（あと{remain}回でロックされます）' if not locked else
                                        f'（{RESET_LOCK_MINUTES}分間ロックしました）')}), 401

        token = secrets.token_urlsafe(32)
        expires = (datetime.datetime.utcnow()
                   + datetime.timedelta(minutes=RESET_TOKEN_MINUTES)).isoformat()
        conn.execute('UPDATE users SET reset_token_hash = ?, reset_token_expires = ?, '
                     'reset_attempts = 0, reset_locked_until = NULL WHERE username = ?',
                     (_hash_token(token), expires, username))
        conn.commit()

        # メール設定済みなら送信し、トークンは応答に含めない。
        # 未設定なら画面に渡す（ここに到達できるのは答えを知っている本人だけ）。
        if _send_reset_mail(r['email'], token, username):
            return jsonify({'sent_to': _mask_email(r['email'])})
        return jsonify({'token': token, 'expires_minutes': RESET_TOKEN_MINUTES})
    finally:
        conn.close()


@app.route('/api/forgot/reset', methods=['POST'])
def api_forgot_reset():
    """ワンタイムトークンでパスワードを再設定する。トークンは単回使用。"""
    payload = request.get_json(silent=True) or {}
    username = (payload.get('username') or '').strip()
    token = payload.get('token') or ''
    new_password = payload.get('new_password') or ''
    if len(new_password) < 8:
        return jsonify({'error': 'パスワードは8文字以上にしてください'}), 400
    conn = _get_db()
    try:
        r = conn.execute('SELECT reset_token_hash, reset_token_expires FROM users '
                         'WHERE username = ?', (username,)).fetchone()
        if not r or not r['reset_token_hash'] or not token:
            return jsonify({'error': '再設定の手続きをやり直してください'}), 400
        if datetime.datetime.utcnow().isoformat() > (r['reset_token_expires'] or ''):
            return jsonify({'error': '有効期限が切れました。最初からやり直してください'}), 400
        if not secrets.compare_digest(_hash_token(token), r['reset_token_hash']):
            return jsonify({'error': '再設定の手続きをやり直してください'}), 400

        # pw_epoch を上げて既存セッションを全て無効化する。
        # 乗っ取られた状態からの復旧が主目的なので、ここは必須。
        conn.execute('UPDATE users SET password_hash = ?, pw_epoch = COALESCE(pw_epoch,0) + 1, '
                     'reset_token_hash = NULL, reset_token_expires = NULL, '
                     'reset_attempts = 0, reset_locked_until = NULL '
                     'WHERE username = ?', (generate_password_hash(new_password), username))
        conn.commit()
        return jsonify({'ok': True})
    finally:
        conn.close()


@app.route('/forgot')
def forgot_page():
    return send_from_directory('static', 'forgot.html')


@app.route('/api/threads-accounts', methods=['GET', 'POST'])
@api_login_required
def api_threads_accounts():
    user = current_username()
    conn = _get_db()
    try:
        if request.method == 'GET':
            return jsonify([_account_public(a) for a in _user_accounts(conn, user)])
        payload = request.get_json(silent=True) or {}
        label = (payload.get('label') or '').strip()[:60]
        if not label:
            return jsonify({'error': '表示名を入力してください'}), 400
        acc_id = secrets.token_hex(8)
        rows = _user_accounts(conn, user)
        is_first = not rows
        conn.execute(
            'INSERT INTO threads_accounts '
            '(id, username, label, threads_user_id, threads_access_token, keywords, sort_order, created_at) '
            'VALUES (?, ?, ?, ?, ?, ?, ?, ?)',
            (acc_id, user, label, '', '', '', len(rows),
             datetime.datetime.utcnow().isoformat()),
        )
        if is_first:
            # 移行時にアカウントが作られなかったユーザー（Threads未設定だった人）の
            # 既存データを最初のアカウントが引き取る。放置すると画面から永久に見えなくなる。
            for t in ('schedule_posts', 'drafts', 'manual_references', 'posts'):
                conn.execute(
                    f'UPDATE {t} SET account_id = ? WHERE username = ? AND account_id IS NULL',
                    (acc_id, user))
        conn.commit()
        a = conn.execute('SELECT * FROM threads_accounts WHERE id = ?', (acc_id,)).fetchone()
        return jsonify(_account_public(a))
    finally:
        conn.close()


@app.route('/api/threads-accounts/<acc_id>', methods=['PATCH', 'DELETE'])
@api_login_required
def api_threads_account_detail(acc_id):
    user = current_username()
    conn = _get_db()
    try:
        a = conn.execute('SELECT * FROM threads_accounts WHERE id = ? AND username = ?',
                         (acc_id, user)).fetchone()
        if not a:
            return jsonify({'error': 'not found'}), 404

        if request.method == 'DELETE':
            # 投稿処理中のものがあると、公開されたのに記録だけ迷子になる
            busy = conn.execute(
                "SELECT COUNT(*) FROM schedule_posts WHERE account_id = ? AND status = 'posting'",
                (acc_id,)).fetchone()[0]
            if busy:
                return jsonify({'error': 'このアカウントは投稿処理中です。完了後に削除してください'}), 409
            # 予約済みのまま未分類に戻すと、cronが拾って「どのアカウントで投稿するか
            # 分からない状態」で公開してしまう。先に下書きへ落として公開対象から外す。
            conn.execute(
                "UPDATE schedule_posts SET status = 'draft', error = ? "
                "WHERE account_id = ? AND username = ? AND status = 'scheduled'",
                ('アカウント削除に伴い予約を解除しました。投稿先を選び直してください。', acc_id, user),
            )
            # データは消さず未分類に戻す。消してしまうと取り返しがつかない
            for t in ('schedule_posts', 'drafts', 'manual_references', 'posts'):
                conn.execute(f'UPDATE {t} SET account_id = NULL WHERE account_id = ? AND username = ?',
                             (acc_id, user))
            conn.execute('DELETE FROM threads_accounts WHERE id = ? AND username = ?', (acc_id, user))
            conn.commit()
            return jsonify({'ok': True})

        payload = request.get_json(silent=True) or {}
        fields, values = [], []
        if 'label' in payload:
            fields.append('label = ?'); values.append((payload['label'] or '').strip()[:60])
        if 'keywords' in payload:
            values.append((payload['keywords'] or '').strip()[:2000]); fields.append('keywords = ?')
        for k in ('threads_user_id', 'threads_access_token'):
            if k in payload:
                # 空白・改行が混ざるとヘッダ例外にトークン断片が残るので除去する
                fields.append(f'{k} = ?'); values.append(re.sub(r'\s', '', str(payload[k] or '')))
        if fields:
            values.extend([acc_id, user])
            conn.execute(f'UPDATE threads_accounts SET {", ".join(fields)} '
                         f'WHERE id = ? AND username = ?', values)
            conn.commit()
        a = conn.execute('SELECT * FROM threads_accounts WHERE id = ? AND username = ?',
                         (acc_id, user)).fetchone()
        return jsonify(_account_public(a))
    finally:
        conn.close()


@app.route('/api/ingest-token', methods=['POST'])
@api_login_required
def api_ingest_token():
    """自分の拡張機能用トークンを返す（未発行ならその場で発行）。
    発行という副作用があるので GET ではなく POST。"""
    conn = _get_db()
    try:
        return jsonify({'token': _issue_ingest_token(conn, current_username())})
    finally:
        conn.close()


@app.route('/api/ingest-token/regenerate', methods=['POST'])
@api_login_required
def api_ingest_token_regenerate():
    """トークンを作り直す。今入っている拡張機能は動かなくなるので、
    ダウンロードし直してもらう必要がある（画面側で明示している）。"""
    user = current_username()
    token = secrets.token_urlsafe(32)
    conn = _get_db()
    try:
        conn.execute('UPDATE users SET ingest_token = ? WHERE username = ?', (token, user))
        conn.commit()
        return jsonify({'token': token})
    finally:
        conn.close()


@app.route('/ingest', methods=['POST', 'OPTIONS'])
def ingest():
    if request.method == 'OPTIONS':
        return '', 204
    owner = _ingest_username()
    if not owner:
        return jsonify({'error': 'unauthorized'}), 401

    payload = request.get_json(silent=True) or {}
    url = (payload.get('url') or '').strip()
    # javascript: のようなスキームを通すと、投稿URLとして画面に描画されたリンクを
    # オーナーがクリックした瞬間にJSが走る（=Ingestトークンだけで /api/settings を盗める）。
    # Threadsの投稿URLは必ず https なので、そこに限定する。
    if not url.startswith('https://'):
        return jsonify({'error': 'url must start with https://'}), 400
    key = _post_key(url)
    if not key:
        return jsonify({'error': 'invalid threads url'}), 400

    # Ingestトークンは配布物（zip）に入る低権限のクレデンシャル。
    # ここから入った値がそのまま画面に描画されると、トークンを持っただけの相手が
    # オーナーのセッションでJSを実行できてしまう（=/api/settings のAPIキー窃取）。
    # 出力側でエスケープしたうえで、入口でも形式と長さを縛る。
    author = (payload.get('author') or '').strip()[:100]
    text = (payload.get('text') or '').strip()[:5000]
    posted_at = (payload.get('posted_at') or '').strip()[:32]
    if posted_at and not re.match(r'^[0-9A-Za-z:\-\.\+/ ]{1,32}$', posted_at):
        posted_at = ''
    keyword = (payload.get('keyword') or '').strip()[:100]
    likes = payload.get('likes')
    reply_count = payload.get('reply_count')
    impressions = payload.get('impressions')
    likes = likes if isinstance(likes, int) and likes >= 0 else None
    reply_count = reply_count if isinstance(reply_count, int) and reply_count >= 0 else None
    impressions = impressions if isinstance(impressions, int) and impressions >= 0 else None
    # 本人の連投(ツリー)本文。文字列の配列だけ受け付け、空要素は除去。JSONで保存。
    replies = payload.get('replies')
    if isinstance(replies, list):
        replies = [str(r).strip() for r in replies if isinstance(r, str) and str(r).strip()]
    else:
        replies = []
    replies_json = json.dumps(replies, ensure_ascii=False) if replies else None

    now = datetime.datetime.utcnow().isoformat()
    conn = _get_db()
    _cleanup_old(conn)
    row = conn.execute('SELECT * FROM posts WHERE post_key = ? AND username = ?',
                       (key, owner)).fetchone()
    # 担当キーワードからどのアカウントの素材か振り分ける。
    # 既に account_id が入っている行は上書きしない（手動で割り当て直した分が
    # 次の収集で勝手に別アカウントへ移動してしまうため）。
    acc_for_post = _account_id_for_keyword(conn, owner, keyword)
    if row:
        conn.execute(
            '''UPDATE posts SET
                 url = ?,
                 author = COALESCE(NULLIF(?, ''), author),
                 text = COALESCE(NULLIF(?, ''), text),
                 posted_at = COALESCE(NULLIF(?, ''), posted_at),
                 keyword = COALESCE(NULLIF(?, ''), keyword),
                 likes = COALESCE(?, likes),
                 reply_count = COALESCE(?, reply_count),
                 impressions = COALESCE(?, impressions),
                 replies_json = COALESCE(?, replies_json),
                 account_id = COALESCE(account_id, ?),
                 last_seen_at = ?
               WHERE post_key = ? AND username = ?''',
            (url, author, text, posted_at, keyword, likes, reply_count, impressions, replies_json,
             acc_for_post, now, key, owner),
        )
    else:
        conn.execute(
            '''INSERT INTO posts
                 (post_key, username, account_id, url, author, text, posted_at, likes, reply_count, impressions, replies_json, keyword, first_seen_at, last_seen_at)
               VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)''',
            (key, owner, acc_for_post, url, author, text, posted_at, likes, reply_count,
             impressions, replies_json, keyword, now, now),
        )
    conn.commit()
    conn.close()
    return jsonify({'ok': True, 'post_key': key})


@app.route('/api/reference-posts')
@api_login_required
def api_reference_posts():
    """自分が収集した投稿の生一覧。Ingestトークンは /ingest 専用にしたので、
    閲覧はセッション（ログイン）のみで行う。"""
    user = current_username()
    limit = min(int(request.args.get('limit', 50)), 200)
    conn = _get_db()
    # 他エンドポイントと同じ _req_account() に寄せる（空白のみの指定も404で統一される）
    req_acc = None
    if request.args.get('account') is not None:
        a = _req_account(conn, user)
        if a is None:
            conn.close(); return jsonify({'error': 'not found'}), 404
        req_acc = a['id']
    rows = conn.execute(
        '''SELECT *, (COALESCE(likes,0) + COALESCE(reply_count,0) + COALESCE(impressions,0)) AS score
           FROM posts WHERE username = ? AND (? IS NULL OR account_id = ?)
           ORDER BY score DESC, last_seen_at DESC LIMIT ?''',
        (user, req_acc, req_acc, limit),
    ).fetchall()
    conn.close()
    return jsonify([dict(r) for r in rows])


@app.route('/api/posts/<post_key>', methods=['DELETE', 'OPTIONS'])
@api_login_required
def delete_post(post_key):
    if request.method == 'OPTIONS':
        return '', 204
    user = current_username()
    conn = _get_db()
    conn.execute('DELETE FROM posts WHERE post_key = ? AND username = ?', (post_key, user))
    conn.commit()
    conn.close()
    return jsonify({'ok': True})


# ---------------------------------------------------------------------------
# 参照投稿ライブラリ（手動登録・自動収集ともユーザーごと。統合して返す）
# ---------------------------------------------------------------------------

@app.route('/api/reference-library', methods=['GET', 'POST', 'OPTIONS'])
@api_login_required
def reference_library():
    if request.method == 'OPTIONS':
        return '', 204
    user = current_username()
    conn = _get_db()
    if request.method == 'GET':
        acc = _req_account(conn, user)
        if acc is None and request.args.get('account'):
            conn.close(); return jsonify({'error': 'not found'}), 404
        acc_id = acc['id'] if acc else None
        manual_rows = conn.execute(
            'SELECT * FROM manual_references WHERE username = ? AND account_id IS ? '
            'ORDER BY created_at DESC', (user, acc_id)
        ).fetchall()
        auto_rows = conn.execute(
            '''SELECT *, (COALESCE(likes,0) + COALESCE(reply_count,0) + COALESCE(impressions,0)) AS score
               FROM posts WHERE username = ? AND account_id IS ?
               ORDER BY score DESC, last_seen_at DESC LIMIT 100''',
            (user, acc_id)
        ).fetchall()
        items = []
        for r in manual_rows:
            reply_texts = [t for t in [r['reply1'], r['reply2']] if t]
            items.append({
                'id': r['id'], 'main': r['main'] or '', 'reply1': r['reply1'] or '', 'reply2': r['reply2'] or '',
                'replyTexts': reply_texts,
                'url': r['url'] or '', 'date': r['date'] or '', 'likes': r['likes'] or 0,
                'replies': r['replies'] or 0, 'impressions': r['impressions'] or 0, 'source': 'manual',
            })
        for r in auto_rows:
            if not r['text']:
                continue
            reply_texts = []
            if r['replies_json']:
                try:
                    reply_texts = [str(x).strip() for x in json.loads(r['replies_json']) if str(x).strip()]
                except Exception:
                    reply_texts = []
            reply_texts = _clean_reply_texts(r['text'], reply_texts)  # 日付プレフィックス・本文重複を除去
            items.append({
                'id': f"auto:{r['post_key']}", 'main': r['text'] or '',
                'reply1': reply_texts[0] if len(reply_texts) > 0 else '',
                'reply2': reply_texts[1] if len(reply_texts) > 1 else '',
                'replyTexts': reply_texts,
                'url': r['url'] or '', 'date': r['posted_at'] or '', 'likes': r['likes'] or 0,
                'replies': r['reply_count'] or 0, 'impressions': r['impressions'] or 0, 'source': 'auto',
            })
        conn.close()
        return jsonify(items)

    # POST: 手動登録
    payload = request.get_json(silent=True) or {}
    main = (payload.get('main') or '').strip()
    if not main:
        conn.close()
        return jsonify({'error': 'main is required'}), 400
    ref_acc = _req_account(conn, user)
    if ref_acc is None and request.args.get('account'):
        conn.close(); return jsonify({'error': 'not found'}), 404
    ref_acc_id = ref_acc['id'] if ref_acc else None
    # 手動登録側も同じ理由で https 限定にする（片方だけ塞いでも意味がない）
    ref_url = (payload.get('url') or '').strip()
    if ref_url and not ref_url.startswith('https://'):
        ref_url = ''
    rid = secrets.token_hex(8)
    now = datetime.datetime.utcnow().isoformat()
    conn.execute(
        '''INSERT INTO manual_references (id, username, account_id, main, reply1, reply2, url, date, likes, replies, impressions, created_at)
           VALUES (?,?,?,?,?,?,?,?,?,?,?,?)''',
        (rid, user, ref_acc_id,
         main, payload.get('reply1', ''), payload.get('reply2', ''), ref_url,
         payload.get('date', ''), int(payload.get('likes') or 0), int(payload.get('replies') or 0),
         int(payload.get('impressions') or 0), now),
    )
    conn.commit()
    conn.close()
    return jsonify({'ok': True, 'id': rid})


@app.route('/api/reference-library/<rid>', methods=['DELETE', 'OPTIONS'])
@api_login_required
def reference_library_delete(rid):
    if request.method == 'OPTIONS':
        return '', 204
    user = current_username()
    conn = _get_db()
    if rid.startswith('auto:'):
        conn.execute('DELETE FROM posts WHERE post_key = ? AND username = ?',
                     (rid[len('auto:'):], user))
    else:
        conn.execute('DELETE FROM manual_references WHERE id = ? AND username = ?', (rid, user))
    conn.commit()
    conn.close()
    return jsonify({'ok': True})


# ---------------------------------------------------------------------------
# 設定（Gemini/Threads APIキー、投稿テンプレート）※ユーザーごと
# ---------------------------------------------------------------------------

@app.route('/api/settings', methods=['GET', 'PUT', 'OPTIONS'])
@api_login_required
def settings_api():
    if request.method == 'OPTIONS':
        return '', 204
    user = current_username()
    conn = _get_db()
    if request.method == 'GET':
        rows = conn.execute('SELECT key, value FROM user_settings WHERE username = ?', (user,)).fetchall()
        conn.close()
        # threads_* はアカウント側(threads_accounts)に移行済み。
        # user_settings にはロールバック用に旧値が残っているが、誰も使わないものを
        # ページを開くたびにブラウザへ平文で送り続ける必要はないので応答から外す。
        public_keys = [k for k in SETTINGS_KEYS if not k.startswith('threads_')]
        data = {k: '' for k in public_keys}
        data['template_rules'] = DEFAULT_TEMPLATE_RULES
        for r in rows:
            if r['key'] in data and r['value'] is not None:
                data[r['key']] = r['value']
        return jsonify(data)

    payload = request.get_json(silent=True) or {}
    # トークン内に改行・空白が混ざると requests が InvalidHeader を投げ、
    # その例外文にヘッダ値の断片が残る。保存時に除去しておく。
    for k in ('threads_access_token', 'threads_user_id', 'gemini_key'):
        if isinstance(payload.get(k), str):
            payload[k] = re.sub(r'\s', '', payload[k])
    for k in SETTINGS_KEYS:
        if k in payload:
            conn.execute(
                'INSERT INTO user_settings (username, key, value) VALUES (?, ?, ?) '
                'ON CONFLICT(username, key) DO UPDATE SET value = excluded.value',
                (user, k, payload[k]),
            )
    conn.commit()
    conn.close()
    return jsonify({'ok': True})


def _get_settings_dict(conn, user):
    rows = conn.execute('SELECT key, value FROM user_settings WHERE username = ?', (user,)).fetchall()
    return {r['key']: r['value'] for r in rows}


# ---------------------------------------------------------------------------
# 投稿スケジュール（Threads運用ツール本体）※ユーザーごと
# ---------------------------------------------------------------------------

def _row_to_schedule_post(r):
    return {
        'id': r['id'], 'date': r['date'], 'time': r['time'],
        'main': r['main'] or '', 'reply1': r['reply1'] or '', 'reply2': r['reply2'] or '',
        'status': r['status'], 'impressions': r['impressions'], 'error': r['error'],
    }


@app.route('/api/schedule-posts', methods=['GET', 'POST', 'OPTIONS'])
@api_login_required
def schedule_posts():
    if request.method == 'OPTIONS':
        return '', 204
    user = current_username()
    conn = _get_db()
    if request.method == 'GET':
        acc = _req_account(conn, user)
        if acc is None and request.args.get('account'):
            conn.close(); return jsonify({'error': 'not found'}), 404
        if acc:
            rows = conn.execute(
                'SELECT * FROM schedule_posts WHERE username = ? AND account_id = ? ORDER BY date, time',
                (user, acc['id'])).fetchall()
        else:
            rows = conn.execute(
                'SELECT * FROM schedule_posts WHERE username = ? AND account_id IS NULL ORDER BY date, time',
                (user,)).fetchall()
        conn.close()
        return jsonify([_row_to_schedule_post(r) for r in rows])

    acc = _req_account(conn, user)
    if acc is None and request.args.get('account'):
        conn.close(); return jsonify({'error': 'not found'}), 404
    acc_id = acc['id'] if acc else None
    payload = request.get_json(silent=True) or {}
    items = payload if isinstance(payload, list) else (payload.get('items') or [payload])
    now = datetime.datetime.utcnow().isoformat()
    created_ids = []
    for it in items:
        pid = secrets.token_hex(8)
        conn.execute(
            '''INSERT INTO schedule_posts (id,username,account_id,date,time,main,reply1,reply2,status,impressions,error,created_at,updated_at)
               VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?)''',
            (pid, user, acc_id, it.get('date', ''), it.get('time', ''), it.get('main', ''), it.get('reply1', ''),
             it.get('reply2', ''), it.get('status', 'draft'), it.get('impressions'), None, now, now),
        )
        created_ids.append(pid)
    conn.commit()
    conn.close()
    return jsonify({'ok': True, 'ids': created_ids})


@app.route('/api/schedule-posts/<pid>', methods=['PATCH', 'DELETE', 'OPTIONS'])
@api_login_required
def schedule_post_detail(pid):
    if request.method == 'OPTIONS':
        return '', 204
    user = current_username()
    conn = _get_db()

    # 投稿処理中(posting)の行は変更も削除も受け付けない。
    # ここを開けておくと status を書き換えて確保ロックを外せてしまい、二重投稿の経路になる。
    # 削除を許すと、投稿は公開されたのに記録だけ消える状態になる。
    cur_row = conn.execute(
        'SELECT status FROM schedule_posts WHERE id = ? AND username = ?', (pid, user)
    ).fetchone()
    if cur_row and cur_row['status'] == 'posting':
        conn.close()
        return jsonify({'error': 'この投稿は現在処理中です。しばらく待ってから操作してください'}), 409

    if request.method == 'DELETE':
        conn.execute('DELETE FROM schedule_posts WHERE id = ? AND username = ?', (pid, user))
        conn.commit()
        conn.close()
        return jsonify({'ok': True})

    payload = request.get_json(silent=True) or {}
    # posting はサーバ内部でのみ設定する状態。クライアントからは指定させない。
    if 'status' in payload and payload.get('status') not in ('draft', 'scheduled'):
        conn.close()
        return jsonify({'error': 'status は draft か scheduled のみ指定できます'}), 400
    fields, values = [], []
    for col in ('date', 'time', 'main', 'reply1', 'reply2', 'status', 'impressions', 'error'):
        if col in payload:
            fields.append(f'{col} = ?')
            values.append(payload[col])
    if fields:
        values.append(datetime.datetime.utcnow().isoformat())
        values.extend([pid, user])
        conn.execute(f'UPDATE schedule_posts SET {", ".join(fields)}, updated_at = ? WHERE id = ? AND username = ?', values)
        conn.commit()
    row = conn.execute('SELECT * FROM schedule_posts WHERE id = ? AND username = ?', (pid, user)).fetchone()
    conn.close()
    if not row:
        return jsonify({'error': 'not found'}), 404
    return jsonify(_row_to_schedule_post(row))


def _redact(text):
    """例外文字列などに混入したトークンを伏字化する。
    URLのクエリに載る形・素の値の両方を潰す（ログとDBとUIに残さないため）。"""
    t = str(text)
    t = re.sub(r'(access_token=)[^&\s\'"]+', r'\1***', t)
    t = re.sub(r'(Bearer\s+)[A-Za-z0-9._\-]+', r'\1***', t)
    return t


def _threads_call(method, path, token, params=None, timeout=20):
    """Threads APIを呼ぶ。トークンはURLではなくAuthorizationヘッダで送る。
    クエリに載せると、requestsの例外文字列にURLごとトークンが入り、
    それがDBのerror列や画面表示に残ってしまうため。"""
    return requests.request(
        method, f'{THREADS_API_BASE}/{path}',
        params=params or {},
        headers={'Authorization': f'Bearer {token}'},
        timeout=timeout,
    )


def _threads_post(user_id, token, text, reply_to_id=None):
    """Threads APIにテキスト投稿する（コンテナ作成→公開の2ステップ）。失敗時は例外を投げる。
    コンテナ作成直後は処理が未完了で公開時に『Media Not Found』(code 24)になるため、
    少し待ってから公開し、Media Not Foundなら数回リトライする。"""
    params = {'media_type': 'TEXT', 'text': text}
    if reply_to_id:
        params['reply_to_id'] = reply_to_id
    r = _threads_call('POST', f'{user_id}/threads', token, params)
    if not r.ok:
        raise RuntimeError(f'コンテナ作成失敗: {r.status_code} {_redact(r.text)[:300]}')
    creation_id = r.json().get('id')
    if not creation_id:
        raise RuntimeError(f'コンテナ作成失敗: creation_idが返りませんでした {_redact(r.text)[:300]}')

    last = ''
    # 最初に3秒待ち、以降は3秒間隔でリトライ（最大5回≒15秒。テキストは通常数秒で公開可能）
    for attempt in range(5):
        time.sleep(3)
        r2 = _threads_call('POST', f'{user_id}/threads_publish', token,
                           {'creation_id': creation_id})
        if r2.ok:
            published_id = r2.json().get('id')
            if published_id:
                return published_id
            last = f'idが返りませんでした {_redact(r2.text)[:200]}'
            continue
        body = _redact(r2.text or '')
        last = f'{r2.status_code} {body[:200]}'
        # Media Not Found（コンテナ処理待ち）はリトライ、それ以外の恒久エラーは即中断
        if 'Media Not Found' in body or '"code":24' in body or 'does not exist' in body:
            continue
        raise RuntimeError(f'公開失敗: {last}')
    raise RuntimeError(f'公開失敗（コンテナ処理待ちでリトライ上限）: {last}')


def _publish_tree(user_id, token, state, main, reply1, reply2, on_progress=None):
    """本文→リプライ1→リプライ2を順に投稿する。stateに投稿済みIDを記録し、
    既に投稿済みの部分はスキップする（再実行時の重複投稿を防ぐ）。stateはその場で更新される。

    on_progress(state) は各投稿の成功直後に呼ばれる。ここでDBへ即コミットすることで、
    途中でworkerが強制終了されても投稿済みIDが失われず、次回実行が本文を再投稿しない。
    （全部終わってから保存する作りだと、中断時に重複投稿が発生する）"""
    def _mark():
        if on_progress:
            on_progress(state)
    if not state.get('main_id'):
        state['main_id'] = _threads_post(user_id, token, main)
        _mark()
    prev = state['main_id']
    if reply1:
        if not state.get('reply1_id'):
            state['reply1_id'] = _threads_post(user_id, token, reply1, reply_to_id=prev)
            _mark()
        prev = state.get('reply1_id', prev)
    if reply2:
        if not state.get('reply2_id'):
            state['reply2_id'] = _threads_post(user_id, token, reply2, reply_to_id=prev)
            _mark()


def _fetch_views(media_id, token):
    """1投稿のviews(インプレッション)を取得する。取得できなければNoneを返す。
    保存済みIDが解決できない場合があるため、呼び出し側で復旧を試みる。"""
    try:
        r = _threads_call('GET', f'{media_id}/insights', token, {'metric': 'views'}, timeout=15)
        if not r.ok:
            return None
        for item in r.json().get('data', []):
            if item.get('name') == 'views':
                vals = item.get('values') or []
                if vals and isinstance(vals[0].get('value'), int):
                    return vals[0]['value']
    except Exception:
        return None
    return None


def _resolve_main_id(user_id, token, main_text):
    """保存済みのmain_idが解決できないとき、本文の先頭一致で実際の投稿IDを探し直す。
    公開時に返るIDが後から参照できないケースが実際に発生しているため。"""
    key = re.sub(r'\s+', '', (main_text or ''))[:30]
    if not key:
        return None
    try:
        r = _threads_call('GET', f'{user_id}/threads', token, {'fields': 'id,text', 'limit': 25}, timeout=15)
        if not r.ok:
            return None
        for d in r.json().get('data', []):
            if re.sub(r'\s+', '', d.get('text') or '')[:30] == key:
                return d.get('id')
    except Exception:
        return None
    return None


def _claim_post(conn, pid, user, allowed_statuses):
    """予約投稿を 'posting' 状態にアトミックに確保する。確保できたら行を返し、
    取れなければNoneを返す（他の実行が処理中）。

    重要: 既存の thread_state を読んでマージする。failed行には中断時点の投稿済みIDが
    入っているため、新規に上書きすると次回が本文から投稿し直して重複投稿になる。"""
    row = conn.execute(
        'SELECT * FROM schedule_posts WHERE id = ? AND username = ?', (pid, user)
    ).fetchone()
    if not row:
        return None
    try:
        state = json.loads(row['thread_state']) if row['thread_state'] else {}
        if not isinstance(state, dict):
            state = {}
    except Exception:
        state = {}
    state['prev_status'] = row['status']
    now = datetime.datetime.utcnow().isoformat()
    cur = conn.execute(
        'UPDATE schedule_posts SET status = ?, thread_state = ?, updated_at = ? '
        'WHERE id = ? AND username = ? AND status IN (%s)'
        % ','.join('?' * len(allowed_statuses)),
        ['posting', json.dumps(state), now, pid, user] + list(allowed_statuses),
    )
    conn.commit()
    if cur.rowcount != 1:
        return None
    return conn.execute(
        'SELECT * FROM schedule_posts WHERE id = ? AND username = ?', (pid, user)
    ).fetchone()


def _progress_saver(conn, pid, user):
    """投稿1つごとに thread_state と updated_at を保存するコールバックを作る。
    updated_at の更新はハートビートを兼ねる（実行中の行が stale とみなされて
    別の実行に横取りされるのを防ぐ）。"""
    def save(state):
        conn.execute(
            'UPDATE schedule_posts SET thread_state = ?, updated_at = ? WHERE id = ? AND username = ?',
            (json.dumps(state), datetime.datetime.utcnow().isoformat(), pid, user),
        )
        conn.commit()
    return save


def _finalize_post(conn, pid, user, state, status, error):
    """投稿処理の終了時に状態を確定する。prev_status は不要になるので取り除く。"""
    state = dict(state or {})
    state.pop('prev_status', None)
    conn.execute(
        'UPDATE schedule_posts SET status = ?, error = ?, thread_state = ?, updated_at = ? '
        'WHERE id = ? AND username = ?',
        (status, error, json.dumps(state), datetime.datetime.utcnow().isoformat(), pid, user),
    )
    conn.commit()


def _restore_post(conn, pid, user, state):
    """確保したが投稿しないと決めた行を、元の状態に戻す。"""
    prev = (state or {}).get('prev_status') or 'scheduled'
    _finalize_post(conn, pid, user, state, prev, None)


@app.route('/api/schedule-posts/<pid>/execute', methods=['POST', 'OPTIONS'])
@api_login_required
def execute_schedule_post(pid):
    if request.method == 'OPTIONS':
        return '', 204
    user = current_username()
    conn = _get_db()
    exists = conn.execute(
        'SELECT id FROM schedule_posts WHERE id = ? AND username = ?', (pid, user)
    ).fetchone()
    if not exists:
        conn.close()
        return jsonify({'error': 'not found'}), 404

    # 二重投稿の防止: 'posting' へアトミックに確保できた場合だけ処理する。
    # ボタン連打・別タブ・API直叩き・cronとの競合を、すべてここで止める。
    row = _claim_post(conn, pid, user, ('scheduled', 'draft', 'failed'))
    if row is None:
        conn.close()
        return jsonify({'error': 'この投稿は現在処理中です'}), 409

    state = json.loads(row['thread_state']) if row['thread_state'] else {}
    user_id, token = _account_credentials(conn, row)

    if not user_id or not token:
        # Threads APIの認証情報が未設定 → デモ動作にフォールバック（85%成功）。
        # 実投稿と区別できるよう error に [デモ] 印を残す（画面にも表示される）。
        if random.random() < 0.85:
            conn.execute(
                'UPDATE schedule_posts SET impressions = ? WHERE id = ? AND username = ?',
                (random.randint(2000, 24000), pid, user),
            )
            _finalize_post(conn, pid, user, state, 'posted',
                           '[デモ] Threads API未設定のため、実際には投稿していません')
        else:
            _finalize_post(conn, pid, user, state, 'failed',
                           '[デモ] ランダム失敗シミュレート（Threads API未設定のため実投稿はしていません）')
        row = conn.execute(
            'SELECT * FROM schedule_posts WHERE id = ? AND username = ?', (pid, user)
        ).fetchone()
        conn.close()
        return jsonify(_row_to_schedule_post(row))

    try:
        _publish_tree(user_id, token, state, row['main'], row['reply1'], row['reply2'],
                      on_progress=_progress_saver(conn, pid, user))
        _finalize_post(conn, pid, user, state, 'posted', None)
    except Exception as e:
        # 途中まで投稿できた分(state)は on_progress で保存済み → 次の再実行は続きから
        _finalize_post(conn, pid, user, state, 'failed', _redact(e)[:500])

    row = conn.execute(
        'SELECT * FROM schedule_posts WHERE id = ? AND username = ?', (pid, user)
    ).fetchone()
    conn.close()
    return jsonify(_row_to_schedule_post(row))


# ---------------------------------------------------------------------------
# cronからの自動実行（予約投稿の公開 + インプレッション回収）
#   Railway の Volume は1サービスにしか接続できないため、cronサービスはDBを直接読めない。
#   そこで cronサービス → このエンドポイント → Web側(Volumeあり)が処理する形にしている。
# ---------------------------------------------------------------------------

DATE_RE = re.compile(r'^\d{4}-\d{2}-\d{2}$')
TIME_RE = re.compile(r'^\d{2}:\d{2}(:\d{2})?$')


def _cron_authorized():
    """未設定なら常に拒否する（fail-closed）。空ヘッダも先に落とす。"""
    key = request.headers.get('X-Cron-Key', '')
    if not CRON_SHARED_KEY or not key:
        return False
    # 非ASCIIのstrを渡すと TypeError で500になるのでバイト列で比較する
    return secrets.compare_digest(key.encode('utf-8', 'ignore'),
                                  CRON_SHARED_KEY.encode('utf-8', 'ignore'))


def _due_rows(conn, now_str, catchup_str):
    """予約時刻が到来した行を返す。日付・時刻の形式が不正な行は対象外。"""
    out = []
    for r in conn.execute("SELECT * FROM schedule_posts WHERE status = 'scheduled'").fetchall():
        d, t = (r['date'] or ''), (r['time'] or '')
        if not DATE_RE.match(d) or not TIME_RE.match(t):
            continue
        when = f'{d} {t[:5]}'
        if when <= now_str:
            out.append((r, when >= catchup_str))
    out.sort(key=lambda x: (x[0]['date'], x[0]['time']))
    return out


def _stale_rows(conn, stale_before):
    """postingのまま放置された行（実行中にworkerが落ちた等）を返す。
    updated_at はUTCのisoformat。JST側の判定文字列と混ぜると辞書順比較が壊れるので、
    ここは必ずUTC同士で比較する。"""
    return conn.execute(
        "SELECT * FROM schedule_posts WHERE status = 'posting' AND updated_at <= ?",
        (stale_before,),
    ).fetchall()


def _run_one(conn, row, user, user_id, token):
    """確保済みの1件を投稿する。戻り値は 'posted' か 'failed'。"""
    pid = row['id']
    state = json.loads(row['thread_state']) if row['thread_state'] else {}
    try:
        _publish_tree(user_id, token, state, row['main'], row['reply1'], row['reply2'],
                      on_progress=_progress_saver(conn, pid, user))
        _finalize_post(conn, pid, user, state, 'posted', None)
        return 'posted'
    except Exception as e:
        _finalize_post(conn, pid, user, state, 'failed', _redact(e)[:500])
        return 'failed'


def _collect_insights(conn):
    """投稿済みの行のインプレッション(views)を更新する。
    保存済みIDが後から参照できない場合があるため、本文の先頭一致で実IDを探し直して修復する。"""
    since = (datetime.datetime.utcnow() - datetime.timedelta(days=INSIGHTS_REFRESH_DAYS)).isoformat()
    updated = 0
    rows = conn.execute(
        "SELECT * FROM schedule_posts WHERE status = 'posted' AND updated_at >= ?", (since,)
    ).fetchall()
    for r in rows:
        try:
            state = json.loads(r['thread_state']) if r['thread_state'] else {}
        except Exception:
            continue
        if (r['error'] or '').startswith('[デモ]'):
            continue
        user_id, token = _account_credentials(conn, r)
        if not user_id or not token:
            continue
        mid = state.get('main_id')
        views = _fetch_views(mid, token) if mid else None
        if views is None:
            fixed = _resolve_main_id(user_id, token, r['main'])
            if fixed and fixed != mid:
                state['main_id'] = fixed
                conn.execute(
                    'UPDATE schedule_posts SET thread_state = ? WHERE id = ? AND username = ?',
                    (json.dumps(state), r['id'], r['username']),
                )
                conn.commit()
                views = _fetch_views(fixed, token)
        if views is not None and views != r['impressions']:
            conn.execute(
                'UPDATE schedule_posts SET impressions = ? WHERE id = ? AND username = ?',
                (views, r['id'], r['username']),
            )
            conn.commit()
            updated += 1
    return updated


@app.route('/api/cron/run-due', methods=['POST'])
def cron_run_due():
    if not CRON_SHARED_KEY:
        return jsonify({'error': 'cron disabled'}), 503
    if not _cron_authorized():
        return jsonify({'error': 'unauthorized'}), 401

    started = time.time()
    now_jst = datetime.datetime.now(JST)
    now_str = now_jst.strftime('%Y-%m-%d %H:%M')
    catchup_str = (now_jst - datetime.timedelta(hours=CRON_CATCHUP_HOURS)).strftime('%Y-%m-%d %H:%M')
    stale_before = (datetime.datetime.utcnow()
                    - datetime.timedelta(minutes=CRON_STALE_MINUTES)).isoformat()

    stats = {'claimed': 0, 'posted': 0, 'failed': 0,
             'skipped_old': 0, 'skipped_invalid': 0, 'recovered': 0, 'deadline_hit': False}
    conn = _get_db()

    # 1) 中断された行の回収。予約由来のものだけ投稿を継続し、
    #    下書き・失敗由来のものは勝手に公開せず元の状態へ戻す。
    for r in _stale_rows(conn, stale_before):
        try:
            state = json.loads(r['thread_state']) if r['thread_state'] else {}
        except Exception:
            state = {}
        prev = state.get('prev_status')
        # 本文が既に公開されている場合は、ツリーを未完のまま放置しない
        if prev != 'scheduled' and not state.get('main_id'):
            cur = conn.execute(
                "UPDATE schedule_posts SET status = ?, thread_state = ?, updated_at = ? "
                "WHERE id = ? AND username = ? AND status = 'posting' AND updated_at <= ?",
                (prev or 'draft', json.dumps({k: v for k, v in state.items() if k != 'prev_status'}),
                 datetime.datetime.utcnow().isoformat(), r['id'], r['username'], stale_before),
            )
            conn.commit()
            if cur.rowcount == 1:
                stats['recovered'] += 1
            continue
        uid, tok = _account_credentials(conn, r)
        if not uid or not tok:
            _finalize_post(conn, r['id'], r['username'], state, 'failed',
                           'この投稿の送信先アカウントに認証情報がありません。'
                           'アカウント設定でThreadsのUser IDとアクセストークンを登録してください。')
            stats['failed'] += 1
            continue
        # 中断行の再取得もアトミックに行う。条件を付けずに更新すると、
        # 2つのcron実行が同じ行を同時に拾って二重投稿になる。
        cur = conn.execute(
            'UPDATE schedule_posts SET updated_at = ? '
            'WHERE id = ? AND username = ? AND status = ? AND updated_at <= ?',
            (datetime.datetime.utcnow().isoformat(), r['id'], r['username'], 'posting', stale_before),
        )
        conn.commit()
        if cur.rowcount != 1:
            continue
        stats['claimed'] += 1
        stats[_run_one(conn, r, r['username'], uid, tok)] += 1

    # 2) 期限が来た予約の投稿
    for r, fresh in _due_rows(conn, now_str, catchup_str):
        if time.time() - started >= CRON_DEADLINE_SEC:
            stats['deadline_hit'] = True
            break
        if stats['claimed'] >= CRON_MAX_PER_RUN:
            break
        user = r['username']
        if not fresh:
            # cronが長時間止まった後に数日分が一斉投稿されるのを防ぐ
            claimed = _claim_post(conn, r['id'], user, ('scheduled',))
            if claimed is None:
                continue
            st = json.loads(claimed['thread_state']) if claimed['thread_state'] else {}
            _finalize_post(conn, r['id'], user, st, 'failed',
                           '予約時刻を%d時間以上過ぎたため自動投稿を見送りました。'
                           '内容を確認して手動で実行してください。' % CRON_CATCHUP_HOURS)
            stats['skipped_old'] += 1
            continue
        uid, tok = _account_credentials(conn, r)
        claimed = _claim_post(conn, r['id'], user, ('scheduled',))
        if claimed is None:
            continue
        stats['claimed'] += 1
        if not uid or not tok:
            # cron経路ではデモ動作を絶対に通さない。
            # 投稿していないのに「投稿済み」と記録されるのを防ぐため。
            st = json.loads(claimed['thread_state']) if claimed['thread_state'] else {}
            _finalize_post(conn, r['id'], user, st, 'failed',
                           'Threads APIの認証情報が未設定です。'
                           '設定ページでユーザーIDとアクセストークンを登録してください。')
            stats['failed'] += 1
            continue
        stats[_run_one(conn, claimed, user, uid, tok)] += 1

    # 3) インプレッションの回収
    try:
        stats['insights_updated'] = _collect_insights(conn)
    except Exception:
        stats['insights_updated'] = 0

    conn.close()
    return jsonify(stats)


# ---------------------------------------------------------------------------
# ドラフト（1件だけ生成、未予約）※ユーザーごと
# ---------------------------------------------------------------------------

@app.route('/api/drafts', methods=['GET', 'POST', 'OPTIONS'])
@api_login_required
def drafts_api():
    if request.method == 'OPTIONS':
        return '', 204
    user = current_username()
    conn = _get_db()
    if request.method == 'GET':
        acc = _req_account(conn, user)
        if acc is None and request.args.get('account'):
            conn.close(); return jsonify({'error': 'not found'}), 404
        if acc:
            rows = conn.execute('SELECT * FROM drafts WHERE username = ? AND account_id = ? '
                                'ORDER BY created_at', (user, acc['id'])).fetchall()
        else:
            rows = conn.execute('SELECT * FROM drafts WHERE username = ? AND account_id IS NULL '
                                'ORDER BY created_at', (user,)).fetchall()
        conn.close()
        return jsonify([{k: r[k] for k in ('id', 'date', 'time', 'main', 'reply1', 'reply2')} for r in rows])

    acc = _req_account(conn, user)
    if acc is None and request.args.get('account'):
        conn.close(); return jsonify({'error': 'not found'}), 404
    payload = request.get_json(silent=True) or {}
    did = secrets.token_hex(8)
    now = datetime.datetime.utcnow().isoformat()
    conn.execute(
        'INSERT INTO drafts (id,username,account_id,date,time,main,reply1,reply2,created_at) '
        'VALUES (?,?,?,?,?,?,?,?,?)',
        (did, user, acc['id'] if acc else None, payload.get('date', ''), payload.get('time', ''),
         payload.get('main', ''), payload.get('reply1', ''), payload.get('reply2', ''), now),
    )
    conn.commit()
    conn.close()
    return jsonify({'ok': True, 'id': did})


@app.route('/api/drafts/<did>', methods=['PATCH', 'DELETE', 'OPTIONS'])
@api_login_required
def draft_detail(did):
    if request.method == 'OPTIONS':
        return '', 204
    user = current_username()
    conn = _get_db()
    if request.method == 'DELETE':
        conn.execute('DELETE FROM drafts WHERE id = ? AND username = ?', (did, user))
        conn.commit()
        conn.close()
        return jsonify({'ok': True})

    payload = request.get_json(silent=True) or {}
    fields, values = [], []
    for col in ('date', 'time', 'main', 'reply1', 'reply2'):
        if col in payload:
            fields.append(f'{col} = ?')
            values.append(payload[col])
    if fields:
        values.extend([did, user])
        conn.execute(f'UPDATE drafts SET {", ".join(fields)} WHERE id = ? AND username = ?', values)
        conn.commit()
    conn.close()
    return jsonify({'ok': True})


# ---------------------------------------------------------------------------
# ページ
# ---------------------------------------------------------------------------

# ---------------------------------------------------------------------------
# 拡張機能の配布（購入者ごとのトークンを埋め込んだzipをその場で生成する）
#   Chromeウェブストアを使わないので、購入者は解凍してデベロッパーモードで読み込む。
#   同梱するファイルは許可リストで固定する（VERIFY.md 等の開発メモを配らないため）。
# ---------------------------------------------------------------------------

EXTENSION_DIR = os.path.join(os.path.dirname(__file__), 'extension_src')
EXTENSION_FILES = ('manifest.json', 'background.js', 'content.js', 'popup.html', 'popup.js', 'icon.png')
EXTENSION_ZIP_ROOT = 'buzz-threads-collector'


def _extension_missing():
    return [f for f in EXTENSION_FILES if not os.path.exists(os.path.join(EXTENSION_DIR, f))]


def _extension_hash():
    """同梱ファイルの内容ハッシュ。buzz-threads-collector/ との同期忘れに気づくため
    /setup に表示する（manifestのversionは固定運用なので検知に使えない）。"""
    h = hashlib.sha256()
    for name in EXTENSION_FILES:
        path = os.path.join(EXTENSION_DIR, name)
        if os.path.exists(path):
            with open(path, 'rb') as f:
                h.update(name.encode())
                h.update(f.read())
    return h.hexdigest()[:12]


@app.route('/setup')
@page_login_required
def setup_page():
    return send_from_directory('static', 'setup.html')


@app.route('/api/extension-info')
@api_login_required
def api_extension_info():
    return jsonify({'hash': _extension_hash(), 'missing': _extension_missing()})


@app.route('/download/extension.zip')
@page_login_required
def download_extension():
    missing = _extension_missing()
    if missing:
        return jsonify({'error': f'配布ファイルが不足しています: {", ".join(missing)}'}), 500
    conn = _get_db()
    try:
        row = conn.execute('SELECT ingest_token FROM users WHERE username = ?',
                           (current_username(),)).fetchone()
    finally:
        conn.close()
    token = row['ingest_token'] if row else None
    if not token:
        return jsonify({'error': 'トークンが未発行です。ページを再読み込みしてください'}), 409

    # request.url_root はプロキシ配下だと http:// になり得る。http だと拡張機能の
    # host_permissions に無く収集が動かないため、順に上書きできるようにする。
    #   1. PUBLIC_BASE_URL（独自ドメインを使う場合に手で指定）
    #   2. RAILWAY_PUBLIC_DOMAIN（Railwayが自動で入れる。テンプレート配布時はこれで足りる）
    #   3. request.url_root（ローカル開発用）
    rail = os.environ.get('RAILWAY_PUBLIC_DOMAIN')
    archive_url = (os.environ.get('PUBLIC_BASE_URL')
                   or (f'https://{rail}' if rail else None)
                   or request.url_root).rstrip('/')
    if archive_url.startswith('http://') and 'localhost' not in archive_url and '127.0.0.1' not in archive_url:
        archive_url = 'https://' + archive_url[len('http://'):]
    # config.js はリポジトリに置かず、ここでメモリ上に組む。
    # ディスクに書かないので、トークン入りファイルがサーバに残らない。
    config = ('self.PRESET = %s;\n' % json.dumps({
        'ingestKey': token,
        'archiveUrl': archive_url,
        'presetId': hashlib.sha256(token.encode()).hexdigest()[:16],
    }, ensure_ascii=False))

    buf = io.BytesIO()
    with zipfile.ZipFile(buf, 'w', zipfile.ZIP_DEFLATED) as z:
        for name in EXTENSION_FILES:
            path = os.path.join(EXTENSION_DIR, name)
            if name == 'manifest.json':
                # host_permissions に自分のドメインが無いと、拡張機能からの送信が
                # 通常のCORS扱いになって必ず失敗する（CORSヘッダは返していない）。
                # config.js の archiveUrl だけ書き換えても収集は1件も動かないので、
                # manifest もここで組み直す。
                with open(path, encoding='utf-8') as f:
                    mf = json.load(f)
                hosts = [h for h in mf.get('host_permissions', [])
                         if 'threads.com' in h or 'threads.net' in h]
                hosts.append(archive_url.rstrip('/') + '/*')
                mf['host_permissions'] = hosts
                z.writestr(f'{EXTENSION_ZIP_ROOT}/{name}',
                           json.dumps(mf, ensure_ascii=False, indent=2))
                continue
            z.write(path, f'{EXTENSION_ZIP_ROOT}/{name}')
        z.writestr(f'{EXTENSION_ZIP_ROOT}/config.js', config)
    buf.seek(0)
    resp = app.response_class(buf.read(), mimetype='application/zip')
    # ファイル名は全員固定。ユーザー名やトークンを含めるとダウンロード履歴やスクショから漏れる
    resp.headers['Content-Disposition'] = f'attachment; filename="{EXTENSION_ZIP_ROOT}.zip"'
    resp.headers['Cache-Control'] = 'no-store'
    return resp


@app.route('/')
@page_login_required
def index():
    return send_from_directory('static', 'tool.html')


@app.route('/login')
def login_page():
    return send_from_directory('static', 'login.html')


@app.route('/archive')
@page_login_required
def archive_viewer():
    return send_from_directory('static', 'archive.html')


@app.route('/ping')
def ping():
    return jsonify({'status': 'ok'})


if __name__ == '__main__':
    # debug=True のまま公開ホストで起動すると、Werkzeugのデバッガ経由で
    # 任意コードを実行される。本番は gunicorn 起動なのでここは通らないが、
    # 誰かが python app.py で立てても事故らないよう既定を False にする。
    app.run(host='0.0.0.0', port=int(os.environ.get('PORT', 5002)),
            debug=os.environ.get('FLASK_DEBUG') == '1')

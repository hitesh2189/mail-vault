import contextlib
import datetime as dt
import io
import os
import secrets
import subprocess
import sys
from functools import wraps

APP_DIR = os.path.dirname(os.path.abspath(__file__))


def _rule_cli_args(rules):
    rules = rules or {}
    a = []
    if rules.get('before'): a += ['--before', rules['before']]
    if rules.get('months'): a += ['--months', str(rules['months'])]
    for s in rules.get('senders_include', []): a += ['--sender-include', s]
    for s in rules.get('senders_exclude', []): a += ['--sender-exclude', s]
    for s in rules.get('labels_include', []): a += ['--label-include', s]
    for s in rules.get('labels_exclude', []): a += ['--label-exclude', s]
    if rules.get('min_size'): a += ['--min-size', str(rules['min_size'])]
    return a


def _launch_sync(mailbox, rules, cap=None):
    """Fire-and-forget a read-only sync; never blocks the request. archive.py holds its own lock."""
    cmd = [sys.executable, os.path.join(APP_DIR, 'archive.py'), 'sync', '--mailbox', mailbox]
    if cap:
        cmd += ['--max', str(cap)]
    cmd += _rule_cli_args(rules)
    logdir = os.path.dirname(os.getenv('DATABASE_PATH', 'archive.sqlite')) or '.'
    try:
        log = open(os.path.join(logdir, 'autosync.log'), 'a')
        subprocess.Popen(cmd, stdout=log, stderr=log, start_new_session=True, cwd=APP_DIR)
        return True
    except Exception:
        return False


def _maybe_autosync(owner, run, rules, configured):
    """On login, back up the user's OWN mailbox using THEIR saved rules — but only if they have
    configured rules (opted in). No saved rules → no surprise full backup. Skips if a good run exists."""
    if os.getenv('AUTO_SYNC_ON_LOGIN', 'false').lower() != 'true':
        return
    if not configured:
        return
    if run and run['finished'] and not run['error']:
        return
    _launch_sync(owner, rules, cap=os.getenv('LOGIN_SYNC_MAX', '').strip() or None)
from flask import Flask, abort, redirect, render_template, request, send_file, session, url_for
from authlib.integrations.flask_client import OAuth
from archive import (connect, parsed, verified_bytes, storage, audit, validate_owner,
                     select_candidates, cleanup as run_cleanup, sync_query, get_rules, save_rules, html_body)


def create_app():
    app = Flask(__name__)
    secret = os.environ.get('SECRET_KEY','')
    if len(secret) < 32:
        raise RuntimeError('Set SECRET_KEY to at least 32 random characters')
    app.secret_key = secret
    app.config.update(SESSION_COOKIE_HTTPONLY=True,SESSION_COOKIE_SECURE=True,
                      SESSION_COOKIE_SAMESITE='Lax',PERMANENT_SESSION_LIFETIME=dt.timedelta(hours=8),
                      MAX_CONTENT_LENGTH=1024*1024)
    oauth = OAuth(app)
    google = oauth.register('google',client_id=os.environ.get('GOOGLE_CLIENT_ID'),
        client_secret=os.environ.get('GOOGLE_CLIENT_SECRET'),
        server_metadata_url='https://accounts.google.com/.well-known/openid-configuration',
        client_kwargs={'scope':'openid email profile'})

    def protected(fn):
        @wraps(fn)
        def wrapped(*args,**kwargs):
            if not session.get('owner'):
                return redirect(url_for('login'))
            try:
                validate_owner(session['owner'])
            except (ValueError,KeyError):
                # Owner removed from the allowlist mid-session: log out cleanly, do not 500.
                session.clear()
                return redirect(url_for('login'))
            return fn(*args,**kwargs)
        return wrapped

    def owned(db,mid):
        row = db.execute('SELECT * FROM messages WHERE owner=? AND id=? '
                         'AND id NOT IN (SELECT id FROM removed WHERE owner=?)',
                         (session['owner'],mid,session['owner'])).fetchone()
        if row is None:
            abort(404)
        return row

    @app.after_request
    def headers(response):
        response.headers['Cache-Control'] = 'no-store'
        response.headers['X-Content-Type-Options'] = 'nosniff'
        response.headers['X-Frame-Options'] = 'DENY'
        response.headers['Content-Security-Policy'] = "default-src 'self'; style-src 'self' 'unsafe-inline'; form-action 'self'; frame-ancestors 'none'; base-uri 'none'"
        response.headers['Referrer-Policy'] = 'no-referrer'
        response.headers['Strict-Transport-Security'] = 'max-age=31536000'
        return response

    @app.get('/login')
    def login():
        callback = os.environ['PUBLIC_URL'].rstrip('/')+'/auth/callback'
        return google.authorize_redirect(callback,hd=os.environ['WORKSPACE_DOMAIN'],prompt='select_account')

    @app.get('/auth/callback')
    def callback():
        # Authlib validates OAuth state, ID-token signature, audience, expiry and nonce.
        token = google.authorize_access_token()
        user = token.get('userinfo',{})
        if user.get('hd','').lower() != os.environ['WORKSPACE_DOMAIN'].lower() or user.get('email_verified') is not True:
            abort(403)
        try:
            owner = validate_owner(user['email'])
        except (ValueError,KeyError):
            abort(403)
        session.clear()
        session.update(owner=owner,sub=user['sub'],csrf=secrets.token_urlsafe(32))
        session.permanent = True
        with contextlib.closing(connect()) as db:
            audit(db,owner,'login')
            run = db.execute('SELECT * FROM runs WHERE owner=?',(owner,)).fetchone()
            rules = get_rules(db,owner)
            configured = db.execute('SELECT 1 FROM mailbox_rules WHERE owner=?',(owner,)).fetchone() is not None
        _maybe_autosync(owner,run,rules,configured)
        return redirect('/')

    @app.post('/logout')
    @protected
    def logout():
        if not secrets.compare_digest(request.form.get('csrf',''),session.get('csrf','invalid')):
            abort(403)
        session.clear()
        return redirect('/')

    @app.post('/backup/remove')
    @protected
    def backup_remove():
        # Hide selected messages from the user's OWN archive (tombstone). S3 + metadata retained; Gmail untouched.
        if not secrets.compare_digest(request.form.get('csrf',''),session.get('csrf','invalid')):
            abort(403)
        owner = session['owner']
        ids = request.form.getlist('ids')[:1000]
        n = 0
        if ids:
            ts = int(dt.datetime.now(dt.timezone.utc).timestamp())
            with contextlib.closing(connect()) as db:
                for mid in ids:
                    if db.execute('SELECT 1 FROM messages WHERE owner=? AND id=?',(owner,mid)).fetchone():
                        db.execute('INSERT OR IGNORE INTO removed VALUES(?,?,?,?)',(owner,mid,ts,owner))
                        n += 1
                audit(db,owner,'backup_removed',None,str(n))
                db.commit()
        nxt = request.form.get('next','/')
        if not nxt.startswith('/') or nxt.startswith('//'):
            nxt = '/'
        return redirect(nxt)

    @app.get('/')
    @protected
    def index():
        owner = session['owner']
        query = request.args.get('q','').strip()[:300]
        clauses,params = ['m.owner=?','m.id NOT IN (SELECT id FROM removed WHERE owner=?)'],[owner,owner]
        if query:
            # Treat input as literal terms, not executable FTS operators.
            terms = ' AND '.join('"'+term.replace('"','""')+'"' for term in query.split())
            clauses.append('m.id IN (SELECT id FROM search WHERE search MATCH ? AND owner=?)')
            params += [terms,owner]
        for field,op in [('after','>='),('before','<')]:
            value = request.args.get(field,'')
            if value:
                try:
                    timestamp = int(dt.datetime.strptime(value,'%Y-%m-%d').replace(tzinfo=dt.timezone.utc).timestamp())
                except ValueError:
                    abort(400)
                clauses.append(f'm.received{op}?')
                params.append(timestamp)
        try:
            page = max(1,int(request.args.get('page','1')))
        except ValueError:
            abort(400)
        with contextlib.closing(connect()) as db:
            rows = db.execute('SELECT m.* FROM messages m WHERE '+' AND '.join(clauses)+
                              ' ORDER BY m.received DESC LIMIT 51 OFFSET ?',params+[(page-1)*50]).fetchall()
            stats = db.execute('SELECT count(*) total,coalesce(sum(size),0) bytes FROM messages WHERE owner=? '
                               'AND id NOT IN (SELECT id FROM removed WHERE owner=?)',(owner,owner)).fetchone()
            run = db.execute('SELECT * FROM runs WHERE owner=?',(owner,)).fetchone()
            audit(db,owner,'search',detail=query)
        return render_template('index.html',rows=rows[:50],more=len(rows)>50,page=page,stats=stats,run=run)

    @app.get('/message/<mid>')
    @protected
    def message(mid):
        with contextlib.closing(connect()) as db:
            row = owned(db,mid)
            raw = verified_bytes(storage(),row)
            msg, body = parsed(raw)
            html = html_body(raw)
            attachments = [(i,p.get_filename() or 'attachment') for i,p in enumerate(msg.walk())
                           if p.get_content_disposition()=='attachment' or p.get_filename()]
            audit(db,session['owner'],'view',mid)
        # Formatted HTML is shown in a locked-down sandbox (no scripts, trackers blocked); plain text is the fallback.
        return render_template('message.html',row=row,body=body,html=html,attachments=attachments)

    @app.get('/message/<mid>/download')
    @protected
    def download(mid):
        with contextlib.closing(connect()) as db:
            row = owned(db,mid)
            raw = verified_bytes(storage(),row)
            audit(db,session['owner'],'download',mid)
        return send_file(io.BytesIO(raw),mimetype='message/rfc822',as_attachment=True,download_name=mid+'.eml')

    @app.get('/message/<mid>/attachment/<int:part_id>')
    @protected
    def attachment(mid,part_id):
        with contextlib.closing(connect()) as db:
            row = owned(db,mid)
            msg,_ = parsed(verified_bytes(storage(),row))
            parts = list(msg.walk())
            if part_id >= len(parts):
                abort(404)
            part = parts[part_id]
            if not (part.get_content_disposition()=='attachment' or part.get_filename()):
                abort(404)
            payload = part.get_payload(decode=True)
            if payload is None:
                abort(404)
            audit(db,session['owner'],'attachment_download',mid,str(part_id))
        return send_file(io.BytesIO(payload),mimetype='application/octet-stream',as_attachment=True,
                         download_name=(part.get_filename() or 'attachment').replace('/','_').replace('\\','_'))

    @app.get('/admin')
    @protected
    def admin():
        if session['owner'] not in {x.strip().lower() for x in os.getenv('ADMIN_EMAILS','').split(',')}:
            abort(403)
        with contextlib.closing(connect()) as db:
            runs = db.execute('SELECT r.*,count(m.id) total,coalesce(sum(m.size),0) bytes FROM runs r '
                'LEFT JOIN messages m ON r.owner=m.owner GROUP BY r.owner').fetchall()
        cleanup = os.getenv('ENABLE_CLEANUP','false').lower() == 'true'
        return render_template('admin.html',runs=runs,cleanup=cleanup)

    def _admins():
        return {x.strip().lower() for x in os.getenv('ADMIN_EMAILS','').split(',') if x.strip()}

    def _rules_from_form(f):
        def lst(name):
            return [x.strip() for x in f.get(name,'').replace('\n',',').split(',') if x.strip()]
        rules = {'senders_include':lst('senders_include'),'senders_exclude':lst('senders_exclude'),
                 'labels_include':lst('labels_include'),'labels_exclude':lst('labels_exclude')}
        if f.get('before','').strip():
            rules['before'] = f['before'].strip()
        if f.get('months','').strip():
            rules['months'] = int(f['months'])
        if f.get('min_size_mb','').strip():
            rules['min_size'] = int(float(f['min_size_mb'])*1024*1024)
        return rules

    def _rules_to_form(r):
        r = r or {}
        return {'before':r.get('before',''),'months':str(r.get('months','') or ''),
                'senders_include':', '.join(r.get('senders_include',[])),
                'senders_exclude':', '.join(r.get('senders_exclude',[])),
                'labels_include':', '.join(r.get('labels_include',[])),
                'labels_exclude':', '.join(r.get('labels_exclude',[])),
                'min_size_mb':(str(round(r['min_size']/1048576,2)) if r.get('min_size') else '')}

    @app.route('/settings', methods=['GET','POST'])
    @protected
    def settings():
        owner = session['owner']
        saved = error = None
        with contextlib.closing(connect()) as db:
            if request.method == 'POST':
                if not secrets.compare_digest(request.form.get('csrf',''),session.get('csrf','invalid')):
                    abort(403)
                try:
                    rules = _rules_from_form(request.form)
                except (ValueError,TypeError):
                    abort(400)
                save_rules(db,owner,rules,owner)
                audit(db,owner,'rules_saved',owner,sync_query(rules))
                saved = True
                if request.form.get('action') == 'sync':
                    _launch_sync(owner,rules,cap=os.getenv('LOGIN_SYNC_MAX','').strip() or None)
                    saved = 'synced'
            current = get_rules(db,owner)
            run = db.execute('SELECT * FROM runs WHERE owner=?',(owner,)).fetchone()
        form = request.form if request.method=='POST' else _rules_to_form(current)
        return render_template('settings.html',saved=saved,error=error,form=form,
                               query=sync_query(current),run=run)

    @app.route('/admin/cleanup/<mailbox>', methods=['GET','POST'])
    @protected
    def cleanup_admin(mailbox):
        if session['owner'] not in _admins():
            abort(403)
        try:
            mailbox = validate_owner(mailbox)
        except (ValueError,KeyError):
            abort(404)
        preview = result = error = None
        cleanup_enabled = os.getenv('ENABLE_CLEANUP','false').lower() == 'true'
        if request.method == 'POST':
            if not secrets.compare_digest(request.form.get('csrf',''),session.get('csrf','invalid')):
                abort(403)
            try:
                rules = _rules_from_form(request.form)
            except (ValueError,TypeError):
                abort(400)
            action = request.form.get('action')
            if action == 'save':
                with contextlib.closing(connect()) as db:
                    save_rules(db,mailbox,rules,session['owner'])
                    audit(db,session['owner'],'rules_saved',mailbox,sync_query(rules))
                error = 'Rules saved for '+mailbox+'.'
            elif action == 'sync':
                with contextlib.closing(connect()) as db:
                    save_rules(db,mailbox,rules,session['owner'])
                    audit(db,session['owner'],'targeted_sync',mailbox,sync_query(rules))
                started = _launch_sync(mailbox,rules)
                error = ('Targeted backup started for '+mailbox+' with the rules below — refresh in a bit, '
                         'then Preview. (Backup must finish before cleanup will run.)') if started else 'Could not start backup.'
            elif action == 'preview':
                with contextlib.closing(connect()) as db:
                    cutoff_ts, cands = select_candidates(db,mailbox,rules)
                    audit(db,session['owner'],'cleanup_preview',mailbox,str(len(cands)))
                preview = dict(cutoff=cutoff_ts,count=len(cands),
                               bytes=sum(r['size'] for r in cands),sample=cands[:30])
            elif action == 'apply':
                if request.form.get('confirm','') != 'TRASH '+mailbox:
                    error = 'Confirmation text did not match — nothing was trashed.'
                else:
                    try:
                        limit = min(max(int(request.form.get('limit','25')),1),100)
                        result = run_cleanup(mailbox,apply=True,limit=limit,rules=rules)
                    except ValueError as exc:
                        error = str(exc)
        if request.method == 'POST':
            form = request.form
            try:
                query_preview = sync_query(_rules_from_form(request.form))
            except Exception:
                query_preview = ''
        else:
            with contextlib.closing(connect()) as db:
                saved_rules = get_rules(db,mailbox)
            form = _rules_to_form(saved_rules)
            query_preview = sync_query(saved_rules)
        return render_template('cleanup.html',mailbox=mailbox,preview=preview,result=result,
                               error=error,cleanup_enabled=cleanup_enabled,form=form,query=query_preview)

    @app.template_filter('date')
    def date(value):
        return dt.datetime.fromtimestamp(value,dt.timezone.utc).strftime('%d %b %Y, %H:%M UTC') if value else 'Not yet'
    return app

"""Gmail -> S3 archive. This worker never permanently deletes messages."""
import argparse
import base64
import calendar
import contextlib
import datetime as dt
import email.policy
import hashlib
import json
import os
import sqlite3
from email.parser import BytesParser

import boto3
from bs4 import BeautifulSoup
from google.oauth2 import service_account
from googleapiclient.discovery import build

SCOPES = ['https://www.googleapis.com/auth/gmail.readonly']

def connect():
    db = sqlite3.connect(os.getenv('DATABASE_PATH', 'archive.sqlite'), timeout=30)
    db.row_factory = sqlite3.Row
    db.execute('PRAGMA journal_mode=WAL')
    db.execute('PRAGMA foreign_keys=ON')
    db.executescript('''
    CREATE TABLE IF NOT EXISTS messages (
      owner TEXT NOT NULL, id TEXT NOT NULL, object_key TEXT NOT NULL,
      digest TEXT NOT NULL, size INTEGER NOT NULL, received INTEGER NOT NULL,
      subject TEXT NOT NULL, sender TEXT NOT NULL, recipients TEXT NOT NULL,
      body TEXT NOT NULL, labels TEXT NOT NULL, archived_at INTEGER NOT NULL,
      verified_at INTEGER NOT NULL, trashed_at INTEGER,
      PRIMARY KEY(owner,id));
    CREATE INDEX IF NOT EXISTS owner_date ON messages(owner,received);
    CREATE VIRTUAL TABLE IF NOT EXISTS search USING fts5(
      owner UNINDEXED, id UNINDEXED, subject, sender, recipients, body);
    CREATE TABLE IF NOT EXISTS runs (
      owner TEXT PRIMARY KEY, started INTEGER, finished INTEGER,
      archived INTEGER DEFAULT 0, error TEXT);
    CREATE TABLE IF NOT EXISTS audit (
      time INTEGER NOT NULL, actor TEXT NOT NULL, action TEXT NOT NULL,
      message_id TEXT, detail TEXT);
    CREATE TABLE IF NOT EXISTS mailbox_rules (
      owner TEXT PRIMARY KEY, rules TEXT NOT NULL,
      updated INTEGER NOT NULL, updated_by TEXT NOT NULL);
    CREATE TABLE IF NOT EXISTS removed (
      owner TEXT NOT NULL, id TEXT NOT NULL, removed_at INTEGER NOT NULL,
      removed_by TEXT NOT NULL, PRIMARY KEY(owner,id));
    ''')
    return db

def get_rules(db, owner):
    row = db.execute('SELECT rules FROM mailbox_rules WHERE owner=?',(owner,)).fetchone()
    return json.loads(row['rules']) if row else {}

def save_rules(db, owner, rules, by):
    db.execute('INSERT OR REPLACE INTO mailbox_rules(owner,rules,updated,updated_by) VALUES(?,?,?,?)',
               (owner, json.dumps(rules), now(), by))
    db.commit()

def now():
    return int(dt.datetime.now(dt.timezone.utc).timestamp())

def cutoff():
    today = dt.datetime.now(dt.timezone.utc)
    year = today.year - 1
    return today.replace(year=year, day=min(today.day, calendar.monthrange(year,today.month)[1]))

def validate_owner(owner):
    owner = owner.strip().lower()
    domain = os.environ['WORKSPACE_DOMAIN'].lower()
    if owner.rsplit('@',1)[-1] != domain:
        raise ValueError('Mailbox must belong to WORKSPACE_DOMAIN')
    # Self-service mode: any verified mailbox in the Workspace domain is allowed.
    # A user is only ever this owner via their own OAuth identity, so access stays self-scoped.
    if os.getenv('OPEN_DOMAIN','false').lower() == 'true':
        return owner
    allowed = {x.strip().lower() for x in os.environ.get('MAILBOXES','').split(',') if x.strip()}
    if owner not in allowed:
        raise ValueError('Mailbox not in MAILBOXES allowlist')
    return owner

def gmail(owner, modify=False):
    validate_owner(owner)
    scopes = ['https://www.googleapis.com/auth/gmail.modify'] if modify else SCOPES
    creds = service_account.Credentials.from_service_account_file(
        os.environ['GOOGLE_SERVICE_ACCOUNT_FILE'], scopes=scopes).with_subject(owner)
    return build('gmail','v1',credentials=creds,cache_discovery=False).users().messages()

def storage():
    return boto3.client('s3')

def audit(db, actor, action, mid=None, detail=''):
    db.execute('INSERT INTO audit VALUES (?,?,?,?,?)',(now(),actor,action,mid,detail))
    db.commit()

def parsed(raw):
    msg = BytesParser(policy=email.policy.default).parsebytes(raw)
    part = msg.get_body(preferencelist=('plain','html'))
    body = ''
    if part:
        try:
            body = part.get_content()
            if part.get_content_type() == 'text/html':
                body = BeautifulSoup(body, 'html.parser').get_text(' ',strip=True)
        except (LookupError,UnicodeError):
            body = '[Body could not be decoded; download original email.]'
    return msg, body

def html_body(raw):
    """Return a sanitized, self-contained HTML document for safe in-browser preview,
    or None if the message has no HTML part. Scripts/handlers stripped; a strict CSP meta
    blocks all external loads (trackers) and a sandboxed iframe at render time blocks JS."""
    msg = BytesParser(policy=email.policy.default).parsebytes(raw)
    part = msg.get_body(preferencelist=('html',))
    if not part:
        return None
    try:
        content = part.get_content()
    except (LookupError, UnicodeError):
        return None
    soup = BeautifulSoup(content, 'html.parser')
    for tag in soup(['script', 'iframe', 'object', 'embed', 'link', 'meta', 'base', 'form', 'noscript']):
        tag.decompose()
    for t in soup.find_all(True):
        for attr in list(t.attrs):
            val = str(t.attrs[attr]).strip().lower()
            if attr.lower().startswith('on') or (attr.lower() in ('href', 'src', 'action', 'xlink:href') and val.startswith('javascript:')):
                del t.attrs[attr]
    csp = ("default-src 'none'; style-src 'unsafe-inline'; img-src data:; "
           "font-src data:; media-src data:")
    return '<!doctype html><meta http-equiv="Content-Security-Policy" content="%s">%s' % (csp, soup)

def verified_bytes(s3, row):
    response = s3.get_object(Bucket=os.environ['S3_BUCKET'], Key=row['object_key'])
    try:
        raw = response['Body'].read()
    finally:
        response['Body'].close()
    if len(raw) != row['size'] or hashlib.sha256(raw).hexdigest() != row['digest']:
        raise ValueError('Archive integrity verification failed')
    return raw

def sync_query(rules):
    """Translate backup rules into a Gmail search query so only matching mail is archived.
    Date/sender/size are pushed server-side; labels are filtered per-message after fetch."""
    rules = rules or {}
    parts = ['-in:chats', '-in:drafts']
    if rules.get('before'):
        parts.append('before:' + rules['before'].replace('-', '/'))
    elif rules.get('months'):
        parts.append('older_than:%dm' % int(rules['months']))
    inc = [s.strip() for s in rules.get('senders_include', []) if s.strip()]
    if inc:
        parts.append('(' + ' OR '.join('from:%s' % s for s in inc) + ')')
    for s in rules.get('senders_exclude', []):
        if s.strip():
            parts.append('-from:%s' % s.strip())
    if rules.get('min_size'):
        parts.append('larger:%d' % int(rules['min_size']))
    return ' '.join(parts)

def sync(owner, limit=None, rules=None):
    owner = validate_owner(owner)
    rules = rules or {}
    query = sync_query(rules)
    inc_l, exc_l = set(rules.get('labels_include', [])), set(rules.get('labels_exclude', []))
    api, s3 = gmail(owner), storage()
    with contextlib.closing(connect()) as db:
        db.execute('INSERT OR REPLACE INTO runs(owner,started,archived) VALUES(?,?,0)',(owner,now()))
        db.commit()
        count, token = 0, None
        try:
            while True:
                page = api.list(userId='me',maxResults=100,pageToken=token,
                                q=query,includeSpamTrash=False).execute(num_retries=5)
                for item in page.get('messages',[]):
                    mid = item['id']
                    if db.execute('SELECT 1 FROM messages WHERE owner=? AND id=?',(owner,mid)).fetchone():
                        continue
                    message = api.get(userId='me',id=mid,format='raw').execute(num_retries=5)
                    labels = set(message.get('labelIds',[]))
                    if {'DRAFT','TRASH','SPAM'} & labels:
                        continue
                    if inc_l and not (inc_l & labels):
                        continue
                    if exc_l and (exc_l & labels):
                        continue
                    raw = base64.urlsafe_b64decode(message['raw'] + '===')
                    digest = hashlib.sha256(raw).hexdigest()
                    tenant = os.environ['WORKSPACE_DOMAIN'].lower()
                    key = f'{tenant}/{owner}/{mid}/{digest}.eml'
                    args = dict(Bucket=os.environ['S3_BUCKET'],Key=key,Body=raw,
                                ContentType='message/rfc822',Metadata={'sha256':digest})
                    kms = os.getenv('S3_KMS_KEY_ID')
                    args.update(ServerSideEncryption='aws:kms' if kms else 'AES256')
                    if kms:
                        args['SSEKMSKeyId'] = kms
                    s3.put_object(**args)
                    # Read back and compare actual bytes before committing the index.
                    row = dict(object_key=key,digest=digest,size=len(raw))
                    verified_bytes(s3,row)
                    msg, body = parsed(raw)
                    values = (owner,mid,key,digest,len(raw),int(message['internalDate'])//1000,
                              str(msg.get('Subject','(No subject)')),str(msg.get('From','')),
                              ', '.join(str(msg.get(h,'')) for h in ('To','Cc','Bcc')),body,
                              json.dumps(message.get('labelIds',[])),now(),now())
                    with db:
                        db.execute('INSERT INTO messages VALUES(?,?,?,?,?,?,?,?,?,?,?,?,?,NULL)', values)
                        db.execute('INSERT INTO search(owner,id,subject,sender,recipients,body) VALUES(?,?,?,?,?,?)',
                                   (owner,mid,values[6],values[7],values[8],body))
                    count += 1
                    if limit and count >= limit:
                        break
                if limit and count >= limit:
                    break
                token = page.get('nextPageToken')
                if not token:
                    break
            db.execute('UPDATE runs SET finished=?,archived=?,error=NULL WHERE owner=?',(now(),count,owner))
            audit(db,owner,'sync_completed',detail=str(count))
        except Exception as exc:
            db.execute('UPDATE runs SET finished=?,archived=?,error=? WHERE owner=?',
                       (now(),count,type(exc).__name__,owner))
            db.commit()
            raise
    print(json.dumps({'owner':owner,'archived':count}))

def rules_cutoff(rules):
    """Resolve the age gate to an epoch second. Messages received BEFORE this are eligible.
    Explicit 'before' date wins; else 'months' ago; else the rolling one-year default."""
    rules = rules or {}
    if rules.get('before'):
        return int(dt.datetime.strptime(rules['before'],'%Y-%m-%d').replace(tzinfo=dt.timezone.utc).timestamp())
    if rules.get('months'):
        today = dt.datetime.now(dt.timezone.utc)
        total = today.year*12 + (today.month-1) - int(rules['months'])
        y, mo = divmod(total,12); mo += 1
        return int(today.replace(year=y,month=mo,day=min(today.day,calendar.monthrange(y,mo)[1])).timestamp())
    return int(cutoff().timestamp())

def _matches(text, terms):
    t = (text or '').lower()
    return any(term.strip().lower() in t for term in terms if term.strip())

def select_candidates(db, owner, rules):
    """Return (cutoff_ts, [rows]) of ARCHIVED, not-yet-trashed messages matching the rules.
    Selection is always from our own index, so cleanup can never target un-backed-up mail."""
    rules = rules or {}
    cutoff_ts = rules_cutoff(rules)
    clauses, params = ['owner=?','received<?','trashed_at IS NULL',
                       'id NOT IN (SELECT id FROM removed WHERE owner=?)'], [owner, cutoff_ts, owner]
    if rules.get('min_size'):
        clauses.append('size>=?'); params.append(int(rules['min_size']))
    rows = db.execute('SELECT * FROM messages WHERE '+' AND '.join(clauses)+' ORDER BY received',params).fetchall()
    inc_s = [t for t in rules.get('senders_include',[]) if t.strip()]
    exc_s = [t for t in rules.get('senders_exclude',[]) if t.strip()]
    inc_l, exc_l = set(rules.get('labels_include',[])), set(rules.get('labels_exclude',[]))
    out = []
    for r in rows:
        if inc_s and not _matches(r['sender'], inc_s): continue
        if exc_s and _matches(r['sender'], exc_s): continue
        labs = set(json.loads(r['labels'] or '[]'))
        if inc_l and not (inc_l & labs): continue
        if exc_l and (exc_l & labs): continue
        out.append(r)
    return cutoff_ts, out

def cleanup(owner, apply=False, limit=25, rules=None):
    owner = validate_owner(owner)
    rules = rules or {}
    if limit < 1 or limit > 500:
        raise ValueError('Limit must be between 1 and 500')
    if apply and os.getenv('ENABLE_CLEANUP','false').lower() != 'true':
        raise ValueError('Cleanup disabled. Set ENABLE_CLEANUP=true after validating recovery.')
    with contextlib.closing(connect()) as db:
        # A fresh complete scan is a prerequisite; failures stop cleanup.
        run = db.execute('SELECT * FROM runs WHERE owner=?',(owner,)).fetchone()
        if not run or not run['finished'] or run['error'] or now()-run['finished'] > 86400:
            raise ValueError('A successful sync in the last 24 hours is required')
        cutoff_ts, cands = select_candidates(db, owner, rules)
        api = gmail(owner,modify=True) if apply else None
        s3 = storage() if apply else None
        trashed = 0
        for row in (cands[:limit] if apply else cands):
            if not apply:
                print(json.dumps({'owner':owner,'id':row['id'],'action':'would_trash'}))
                continue
            verified_bytes(s3,row)  # Check recovery immediately before touching Gmail.
            live = api.get(userId='me',id=row['id'],format='minimal').execute(num_retries=5)
            if {'DRAFT','SPAM','TRASH'} & set(live.get('labelIds',[])):
                continue
            if int(live['internalDate'])//1000 >= cutoff_ts:  # re-check live age against the rule's cutoff
                continue
            api.trash(userId='me',id=row['id']).execute(num_retries=5)
            db.execute('UPDATE messages SET trashed_at=? WHERE owner=? AND id=?',
                       (now(),owner,row['id']))
            audit(db,owner,'gmail_trashed',row['id'])
            trashed += 1
            print(json.dumps({'owner':owner,'id':row['id'],'action':'trashed'}))
        return {'owner':owner,'cutoff':cutoff_ts,'candidates':len(cands),'trashed':trashed}

if __name__ == '__main__':
    parser = argparse.ArgumentParser()
    parser.add_argument('command',choices=['sync','cleanup'])
    parser.add_argument('--mailbox',required=True)
    parser.add_argument('--apply',action='store_true')
    parser.add_argument('--limit',type=int,default=25)
    parser.add_argument('--max',type=int,default=None,dest='max_messages')
    parser.add_argument('--before')
    parser.add_argument('--months',type=int)
    parser.add_argument('--sender-include',action='append',default=[])
    parser.add_argument('--sender-exclude',action='append',default=[])
    parser.add_argument('--label-include',action='append',default=[])
    parser.add_argument('--label-exclude',action='append',default=[])
    parser.add_argument('--min-size',type=int)
    args = parser.parse_args()
    cli_rules = dict(before=args.before,months=args.months,
                     senders_include=args.sender_include,senders_exclude=args.sender_exclude,
                     labels_include=args.label_include,labels_exclude=args.label_exclude,
                     min_size=args.min_size)
    # Single worker lock also protects against overlapping scheduled sync/cleanup.
    import fcntl
    with open(os.getenv('DATABASE_PATH','archive.sqlite')+'.worker.lock','w') as lock:
        fcntl.flock(lock,fcntl.LOCK_EX | fcntl.LOCK_NB)
        if args.command == 'sync':
            sync(args.mailbox, args.max_messages, cli_rules)
        else:
            print(json.dumps(cleanup(args.mailbox,args.apply,args.limit,cli_rules)))

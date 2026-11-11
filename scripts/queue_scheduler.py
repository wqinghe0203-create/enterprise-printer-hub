"""Local transactional queue. Never put this database on a network share."""
import json
import os
import sqlite3
import hashlib
from contextlib import contextmanager
from datetime import datetime
from pathlib import Path

from .config import BASE_DIR, get_queue_config, get_printer, get_default_printer_id
from .runtime_paths import ensure_directory

DATA_DIR = os.path.join(BASE_DIR, 'data')
STATE_FILE = os.path.join(DATA_DIR, 'queue_state.json')
ACTIVE = ('queued', 'converting', 'sending', 'printing', 'paused', 'submitted', 'unknown')
TERMINAL = ('completed', 'canceled', 'failed', 'unconfirmed')


def now():
    return datetime.now().strftime('%Y-%m-%d %H:%M:%S')


class QueueScheduler:
    def __init__(self):
        self.cfg = get_queue_config()
        self.db_path = str(Path(STATE_FILE).with_suffix('.sqlite3'))
        ensure_directory(Path(self.db_path).parent)
        with self.transaction() as db:
            db.execute('CREATE TABLE IF NOT EXISTS meta (key TEXT PRIMARY KEY, value TEXT NOT NULL)')
            db.execute('CREATE TABLE IF NOT EXISTS jobs (id INTEGER PRIMARY KEY, request_id TEXT UNIQUE, body TEXT NOT NULL)')
            if not db.execute("SELECT 1 FROM meta WHERE key='initialized'").fetchone():
                next_id = 101
                if Path(STATE_FILE).exists():
                    try:
                        raw = Path(STATE_FILE).read_bytes()
                        data = json.loads(raw)
                        jobs = data['jobs']
                        next_id = data['next_id']
                        if not isinstance(jobs, list) or type(next_id) is not int:
                            raise ValueError('队列结构无效')
                        ids = [j['id'] for j in jobs]
                        if any(type(i) is not int or i < 1 for i in ids) or len(ids) != len(set(ids)):
                            raise ValueError('任务编号无效或重复')
                        if any(not isinstance(j.get('status'), str) or not j.get('printer_id') for j in jobs):
                            raise ValueError('任务缺少状态或设备')
                        backup = Path(STATE_FILE + '.pre-sqlite.bak')
                        if backup.exists() and backup.read_bytes() != raw:
                            raise ValueError('迁移备份与原记录不一致，请人工核查')
                        if not backup.exists():
                            with backup.open('xb') as output:
                                output.write(raw)
                                output.flush()
                                os.fsync(output.fileno())
                        for job in jobs:
                            if job['status'] == 'printing' and not job.get('hardware_job_id'):
                                job.update(status='unknown', error_msg='升级前发送中断，结果待核查；勿重复提交')
                            db.execute('INSERT INTO jobs VALUES (?, ?, ?)', (job['id'], job.get('request_id'), json.dumps(job, ensure_ascii=False)))
                        next_id = max([101, next_id] + [i + 1 for i in ids])
                    except Exception as exc:
                        raise RuntimeError('旧队列迁移失败，原记录已保留，未启用空队列: ' + str(exc)) from exc
                db.execute("INSERT INTO meta VALUES ('next_id', ?)", (str(next_id),))
                db.execute("INSERT INTO meta VALUES ('initialized', '1')")

    @contextmanager
    def transaction(self):
        db = sqlite3.connect(self.db_path, timeout=30)
        try:
            db.execute('PRAGMA synchronous=FULL')
            db.execute('BEGIN IMMEDIATE')
            yield db
            db.commit()
        except BaseException:
            db.rollback()
            raise
        finally:
            db.close()

    @staticmethod
    def _decode(body):
        job = json.loads(body)
        if job.get('status') == 'completed' and not job.get('completion_verified'):
            job.update(status='unconfirmed', error_msg='旧版本记录缺少硬件完成确认，实际出纸结果未核实')
        return job

    def _all(self, db):
        return [self._decode(row[0]) for row in db.execute('SELECT body FROM jobs ORDER BY id')]

    @staticmethod
    def _write(db, job):
        db.execute('UPDATE jobs SET body=? WHERE id=?', (json.dumps(job, ensure_ascii=False), job['id']))

    def enqueue(self, filepath, user, printer_id=None, filename=None, priority=50,
                pages=1, duplex='duplex_long', color_mode='auto', copies=1,
                request_id=None, needs_conversion=False):
        pid = printer_id or get_default_printer_id()
        printer = get_printer(pid)
        with self.transaction() as db:
            if request_id:
                row = db.execute('SELECT body FROM jobs WHERE request_id=?', (request_id,)).fetchone()
                if row:
                    return self._decode(row[0])
            jid = int(db.execute("SELECT value FROM meta WHERE key='next_id'").fetchone()[0])
            job = dict(id=jid, filename=filename or os.path.basename(filepath), filepath=os.path.abspath(filepath),
                       user=user or '未知用户', printer_id=pid, printer_name=printer.get('name', '打印机'),
                       printer_location=printer.get('location', ''), priority=max(1, min(100, int(priority))),
                       pages=pages, duplex=duplex, color_mode=color_mode, copies=copies, status='queued',
                       created_at=now(), updated_at=now(), error_msg=None, request_id=request_id,
                       needs_conversion=needs_conversion, version=1)
            db.execute('INSERT INTO jobs VALUES (?, ?, ?)', (jid, request_id, json.dumps(job, ensure_ascii=False)))
            db.execute("UPDATE meta SET value=? WHERE key='next_id'", (str(jid + 1),))
            return job

    def get_job(self, job_id):
        with self.transaction() as db:
            row = db.execute('SELECT body FROM jobs WHERE id=?', (int(job_id),)).fetchone()
            return self._decode(row[0]) if row else None

    def get_request(self, request_id):
        with self.transaction() as db:
            row = db.execute('SELECT body FROM jobs WHERE request_id=?', (request_id,)).fetchone()
            return self._decode(row[0]) if row else None

    def get_jobs(self, printer_id=None, include_completed=False, limit=50):
        with self.transaction() as db:
            jobs = [j for j in self._all(db) if not printer_id or j['printer_id'] == printer_id]
        active = sorted((j for j in jobs if j['status'] in ACTIVE), key=lambda j: (-j.get('priority', 50), j['id']))
        if not include_completed:
            return active
        finished = sorted((j for j in jobs if j['status'] in TERMINAL), key=lambda j: (j.get('updated_at', ''), j['id']), reverse=True)
        return (active + finished)[:limit] if limit is not None else active + finished

    def history(self, printer_id=None, page=1, size=5, search='', user='', status='', start='', end=''):
        jobs = [j for j in self.get_jobs(printer_id, True, None) if j['status'] in TERMINAL]
        jobs = [j for j in jobs if (not search or search.casefold() in j['filename'].casefold() or search.lstrip('#') == str(j['id']))
                and (not user or user.casefold() in j['user'].casefold()) and (not status or status == j['status'])
                and (not start or j['created_at'][:10] >= start) and (not end or j['created_at'][:10] <= end)]
        size = size if size in (5, 10, 20) else 5
        pages = max(1, (len(jobs) + size - 1) // size)
        page = max(1, min(page, pages))
        return dict(items=jobs[(page-1)*size:page*size], total=len(jobs), pages=pages, page=page, size=size)

    def record_delivery(self, job_id, status, expected=None, expected_version=None, **details):
        with self.transaction() as db:
            row = db.execute('SELECT body FROM jobs WHERE id=?', (int(job_id),)).fetchone()
            if not row:
                raise ValueError(f'找不到任务 #{job_id}')
            job = self._decode(row[0])
            if (expected is not None and job['status'] not in expected) or (expected_version is not None and job.get('version', 0) != expected_version):
                raise ValueError('任务状态已变化，请刷新后核查')
            if job['status'] in TERMINAL and status != job['status']:
                raise ValueError('任务已结束，请刷新后核查')
            job.update(details)
            job.update(status=status, updated_at=now(), version=job.get('version', 0)+1)
            self._write(db, job)
            return job

    def set_priority(self, job_id, new_priority):
        try:
            job = self.record_delivery(job_id, 'queued', expected=('queued',), priority=max(1, min(100, int(new_priority))))
            return dict(success=True, job=job, priority=job['priority'], job_id=job_id)
        except ValueError as exc:
            return dict(success=False, error=str(exc))

    def cut_in_line(self, job_id):
        return self.set_priority(job_id, 100)

    def cancel_job(self, job_id, expected=None, expected_version=None):
        try:
            job = self.record_delivery(job_id, 'canceled', expected=expected or ('queued', 'paused', 'converting'), expected_version=expected_version, dispatch_active=False)
            return dict(success=True, job=job, job_id=job_id, status=job['status'])
        except ValueError as exc:
            return dict(success=False, error=str(exc))

    def pause_job(self, job_id):
        return self.record_delivery(job_id, 'paused', expected=('queued',))

    def resume_job(self, job_id):
        return self.record_delivery(job_id, 'queued', expected=('paused',))

    def mark_printing(self, job_id):
        return self.record_delivery(job_id, 'printing', expected=('queued',))

    def mark_completed(self, job_id):
        return self.record_delivery(job_id, 'completed')

    def mark_failed(self, job_id, error_msg):
        return self.record_delivery(job_id, 'failed', error_msg=str(error_msg), dispatch_active=False)

    def get_next_job(self, printer_id=None):
        return next((j for j in self.get_jobs(printer_id) if j['status'] == 'queued'), None)

    @contextmanager
    def dispatch_slot(self, printer_id):
        """OS lock spans conversion and network I/O, released even after process death."""
        name = self.db_path + '.' + hashlib.sha256(printer_id.encode()).hexdigest()[:16] + '.lock'
        with open(name, 'a+b') as file:
            file.seek(0, 2)
            if not file.tell():
                file.write(b'0')
                file.flush()
            file.seek(0)
            acquired = False
            try:
                if os.name == 'nt':
                    import msvcrt
                    msvcrt.locking(file.fileno(), msvcrt.LK_NBLCK, 1)
                else:
                    import fcntl
                    fcntl.flock(file, fcntl.LOCK_EX | fcntl.LOCK_NB)
                acquired = True
            except OSError:
                pass
            try:
                if acquired:
                    with self.transaction() as db:
                        for job in self._all(db):
                            if job['printer_id'] == printer_id and job.get('dispatch_active'):
                                job.update(status='unknown', dispatch_active=False, updated_at=now(),
                                           error_msg='处理进程已中断，结果待核查；勿重复提交')
                                self._write(db, job)
                yield acquired
            finally:
                if acquired:
                    file.seek(0)
                    if os.name == 'nt':
                        msvcrt.locking(file.fileno(), msvcrt.LK_UNLCK, 1)
                    else:
                        fcntl.flock(file, fcntl.LOCK_UN)

    def claim_next_job(self, printer_id):
        """Caller must hold dispatch_slot for the entire delivery."""
        with self.transaction() as db:
            jobs = [j for j in self._all(db) if j['printer_id'] == printer_id and j['status'] == 'queued']
            if not jobs:
                return None
            job = min(jobs, key=lambda j: (-j.get('priority', 50), j['id']))
            job.update(status='converting' if job.get('needs_conversion') else 'sending', dispatch_active=True,
                       updated_at=now(), version=job.get('version', 0)+1)
            self._write(db, job)
            return job

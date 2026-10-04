import hashlib
import json
import threading
from datetime import datetime, timedelta, timezone
from zoneinfo import ZoneInfo

from .clients import Clients, DoubanError, RequestGate, SyncError, parse_subject
from .db import now


def scope_for(config):
    return hashlib.sha256(f'{config.seerr_url}|{config.douban_user}'.encode()).hexdigest()


class SyncService:
    def __init__(self, store, clients_factory=Clients):
        self.store = store
        self.clients_factory = clients_factory
        self.lock = threading.Lock()
        self.validation_lock = threading.Lock()
        self.douban_gate = RequestGate()
        self.active_config = None
        self.stop = threading.Event()
        self.thread = None
        self.next_run = None
        self.reschedule()

    @property
    def running(self):
        return self.lock.locked()

    def reschedule(self):
        with self.store.config_lock:
            config = self.store.config()
            self.next_run = datetime.now(timezone.utc) + timedelta(minutes=config.interval_minutes) if config.enabled and config.ready() else None

    def start(self, preview=False):
        if not self.lock.acquire(blocking=False):
            raise SyncError('已有同步任务正在运行，请等待完成')
        try:
            with self.store.config_lock:
                config = self.store.config()
                mappings = {row['subject']: json.loads(row['value']) for row in self.store.rows('SELECT subject,value FROM mappings')}
                if not config.ready():
                    raise SyncError('请先配置豆瓣用户、Cookie 和 Seerr，并开启至少一种媒体类型')
            run_id = self.store.execute("INSERT INTO runs(started,preview,state) VALUES (?,?,'running')", (now(), int(preview)))
            self.stop.clear()
            self.active_config = config
            self.thread = threading.Thread(target=self.run, args=(run_id, config, preview, mappings), daemon=True)
            self.thread.start()
            return run_id
        except Exception:
            self.active_config = None
            self.lock.release()
            raise

    def run(self, run_id, config, preview, mappings):
        client = None
        processed, errors = 0, 0
        state, message = 'complete', ''
        try:
            client = self.clients_factory(config, self.stop)
            if isinstance(client, Clients):
                client.gate = self.douban_gate
            client.verify_douban()
            cutoff = datetime.now(ZoneInfo('Asia/Shanghai')).date() - timedelta(days=config.history_days - 1)
            scope = scope_for(config)
            for entry in client.wish(cutoff):
                if self.stop.is_set():
                    raise SyncError('用户停止任务；已提交的请求保留')
                previous = self.store.rows('SELECT * FROM items WHERE scope=? AND subject=?', (scope, entry['subject']))
                # Success is durable across restarts; preview never marks an item as submitted.
                if previous and previous[0]['state'] in ('submitted', 'existing'):
                    continue
                match = None
                try:
                    if not entry['marked']:
                        raise SyncError('无法读取想看日期，不能确认历史范围，请检查豆瓣条目')
                    if entry['subject'] in mappings:
                        match = mappings[entry['subject']]
                    else:
                        details = parse_subject(client.douban(f"https://movie.douban.com/subject/{entry['subject']}/"))
                        if not (config.movies if details['media_type'] == 'movie' else config.tv):
                            self.store.item(scope, entry, 'disabled', '该媒体类型同步已关闭', details)
                            continue
                        match = client.match(details)
                    if not (config.movies if match['media_type'] == 'movie' else config.tv):
                        self.store.item(scope, entry, 'disabled', '该媒体类型同步已关闭', match)
                        continue
                    result, detail = client.request(match, preview)
                    # Even an already-existing item observed in preview is rechecked in a real run.
                    self.store.item(scope, entry, 'preview' if preview else result, detail, match)
                except DoubanError:
                    raise
                except SyncError as error:
                    errors += 1
                    self.store.item(scope, entry, 'failed', str(error), match)
                processed += 1
                self.store.execute('UPDATE runs SET processed=? WHERE id=?', (processed, run_id))
            state = 'partial' if errors else 'complete'
            message = f'处理 {processed} 条，{errors} 条需要处理' + ('；预览未提交请求' if preview else '')
        except SyncError as error:
            state, message = 'stopped' if self.stop.is_set() else 'failed', str(error)
        except Exception:
            # Never persist raw upstream exceptions: they can contain URLs, tokens or response bodies.
            state, message = 'failed', '内部处理异常，请检查服务版本与数据格式'
        finally:
            if client:
                client.close()
            self.store.execute('UPDATE runs SET finished=?,state=?,message=?,processed=? WHERE id=?', (now(), state, message, processed, run_id))
            if not preview:
                self.reschedule()
            self.active_config = None
            self.lock.release()

    def tick(self):
        # Serialize the scheduling decision with configuration writes so disabling
        # the timer cannot race a stale scheduling decision.
        with self.store.config_lock:
            if self.next_run and datetime.now(timezone.utc) >= self.next_run and not self.running:
                try:
                    self.start()
                except SyncError:
                    self.reschedule()

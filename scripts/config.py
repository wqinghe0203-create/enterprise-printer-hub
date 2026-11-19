# -*- coding: utf-8 -*-
"""
多打印机配置加载与管理模块
支持企业级多打印机注册、动态切换与默认路由
"""
import os
import json
import hashlib
import tempfile
import sqlite3
from pathlib import Path
from contextlib import contextmanager
from typing import List, Dict, Any, Optional

from .runtime_paths import ensure_directory

BASE_DIR = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
CONFIG_PATH = os.path.join(BASE_DIR, "config.json")
PRINTER_DB_PATH = os.path.join(BASE_DIR, "data", "printers.sqlite3")

DEFAULT_CONFIG = {
    "system": {
        "title": "企业智能文印管理中心",
        "default_printer_id": "canon_c3130l_27f"
    },
    "printers": [
        {
            "id": "canon_c3130l_27f",
            "name": "佳能 C3130L 总部复合机",
            "location": "27F 总部",
            "ip": "10.1.16.228",
            "gateway": "10.1.16.254",
            "ipp_port": 631,
            "raw_port": 9100,
            "web_port": 80,
            "remote_ui_port": 8000,
            "ipp_path": "/ipp/print",
            "model": "Canon imageRUNNER C3130L",
            "series": "Canon imageRUNNER C 系列",
            "color_supported": True,
            "duplex_supported": True,
            "official_driver_url": "https://www.canon.com.cn/supports/download/sims/list/slist?fileTypeId=23&categoryId=11&seriesId=57&modelId=2004&OSName=Windows%2011&channel=8"
        }
    ],
    "server": {
        "web_ui_port": 18828,
        "auto_refresh_sec": 3,
        "allow_lan_access": True
    },
    "converter": {
        "mode": "gotenberg",
        "gotenberg_url": "http://127.0.0.1:3000",
        "timeout_sec": 120,
        "max_upload_mb": 50
    },
    "queue": {
        "default_priority": 50,
        "max_priority": 100,
        "min_priority": 1,
        "retention_history_count": 50
    }
}


def _config_source():
    path = Path(CONFIG_PATH)
    if path.exists():
        return path
    return path.with_name("config.example.json")


def load_config() -> Dict[str, Any]:
    try:
        return json.loads(_config_source().read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return DEFAULT_CONFIG


def _validate_printer_config(cfg, migrate_single_default=False):
    if not isinstance(cfg, dict) or not isinstance(cfg.get('printers'), list) or not isinstance(cfg.get('system'), dict):
        raise ValueError('设备配置结构不完整')
    ids = [printer.get('id') for printer in cfg['printers'] if isinstance(printer, dict)]
    if len(ids) != len(cfg['printers']) or not all(isinstance(pid, str) and pid for pid in ids) or len(ids) != len(set(ids)):
        raise ValueError('设备标识无效或重复')
    if cfg['system'].get('default_printer_id') not in ids:
        if migrate_single_default and len(ids) == 1:
            cfg['system']['default_printer_id'] = ids[0]
        else:
            raise ValueError('默认打印机配置无效；请重新选择默认设备')
    endpoints = [printer_endpoint(printer) for printer in cfg['printers']]
    if any(not isinstance(ip, str) or not ip or not 1 <= port <= 65535 or not isinstance(path, str)
           for ip, port, path in endpoints):
        raise ValueError('设备连接配置无效')
    if len(endpoints) != len(set(endpoints)):
        raise ValueError('设备地址、端口和路径重复')


@contextmanager
def _printer_transaction():
    ensure_directory(Path(PRINTER_DB_PATH).parent)
    db = sqlite3.connect(PRINTER_DB_PATH, timeout=30)
    try:
        db.execute('PRAGMA synchronous=FULL')
        db.execute('BEGIN IMMEDIATE')
        db.execute('CREATE TABLE IF NOT EXISTS printers ('
                   'id TEXT PRIMARY KEY, ip TEXT NOT NULL, ipp_port INTEGER NOT NULL, '
                   'ipp_path TEXT NOT NULL, body TEXT NOT NULL, UNIQUE(ip, ipp_port, ipp_path))')
        db.execute('CREATE TABLE IF NOT EXISTS printer_settings (key TEXT PRIMARY KEY, value TEXT NOT NULL)')
        if not db.execute("SELECT 1 FROM printer_settings WHERE key='initialized'").fetchone():
            source = _config_source()
            try:
                raw = source.read_bytes()
                cfg = json.loads(raw)
                _validate_printer_config(cfg, migrate_single_default=True)
            except (OSError, ValueError, TypeError) as exc:
                raise ValueError('打印机首次迁移失败，请检查原 config.json 或 config.example.json；原配置已保留') from exc
            backup = Path(PRINTER_DB_PATH).with_name('printers.config.pre-sqlite.json')
            if backup.exists() and backup.read_bytes() != raw:
                raise ValueError('设备迁移备份与当前配置不一致，请核查后重试')
            if not backup.exists():
                _atomic_write(backup, raw)
            _save_printers(db, cfg)
            db.execute("INSERT INTO printer_settings VALUES ('initialized', '1')")
        yield db
        db.commit()
    except BaseException:
        db.rollback()
        raise
    finally:
        db.close()


def _printer_config(db):
    cfg = load_config()
    printers = [json.loads(row[0]) for row in db.execute('SELECT body FROM printers ORDER BY rowid')]
    default = db.execute("SELECT value FROM printer_settings WHERE key='default_printer_id'").fetchone()
    result = {**cfg, 'printers': printers, 'system': {**cfg.get('system', {}), 'default_printer_id': default[0] if default else None}}
    _validate_printer_config(result)
    return result


def load_config_strict():
    """Read the server's device catalog. Legacy JSON is imported only once."""
    with _printer_transaction() as db:
        return _printer_config(db)


def _atomic_write(path, content):
    temporary = None
    try:
        with tempfile.NamedTemporaryFile(mode='wb', dir=str(Path(path).parent), prefix='.printer-config-', delete=False) as file:
            temporary = file.name
            file.write(content)
            file.flush()
            os.fsync(file.fileno())
        os.replace(temporary, path)
    finally:
        if temporary and os.path.exists(temporary):
            os.unlink(temporary)


def _save_printers(db, cfg):
    _validate_printer_config(cfg)
    db.execute('DELETE FROM printers')
    for printer in cfg['printers']:
        ip, port, path = printer_endpoint(printer)
        db.execute('INSERT INTO printers VALUES (?, ?, ?, ?, ?)',
                   (printer['id'], ip, port, path, json.dumps(printer, ensure_ascii=False)))
    db.execute("INSERT OR REPLACE INTO printer_settings VALUES ('default_printer_id', ?)",
               (cfg['system']['default_printer_id'],))


def save_config(cfg: Dict[str, Any]):
    """Compatibility entry point for explicitly saving a complete configuration."""
    with _printer_transaction() as db:
        _save_printers(db, cfg)
        if Path(CONFIG_PATH).exists():
            _atomic_write(CONFIG_PATH + '.bak', Path(CONFIG_PATH).read_bytes())
        _atomic_write(CONFIG_PATH, (json.dumps(cfg, ensure_ascii=False, indent=2) + '\n').encode('utf-8'))


def printer_endpoint(printer):
    return (printer.get('ip'), int(printer.get('ipp_port', 631)), printer.get('ipp_path', '/ipp/print'))


def printer_connection_version(printer):
    return hashlib.sha256(json.dumps(printer_endpoint(printer)).encode('utf-8')).hexdigest()


def editable_printer(printer):
    ip, port, path = printer_endpoint(printer)
    fields = {'id': printer['id'], 'name': printer.get('name') or '', 'location': printer.get('location') or '',
              'ip': ip, 'ipp_port': port, 'ipp_path': path}
    return {**fields, 'version': hashlib.sha256(json.dumps(fields).encode('utf-8')).hexdigest()}


class PrinterEditConflict(ValueError):
    pass


def update_printer(printer_id, version, changes):
    with _printer_transaction() as db:
        cfg = _printer_config(db)
        existing = next((printer for printer in cfg['printers'] if printer['id'] == printer_id), None)
        if existing is None or editable_printer(existing)['version'] != version:
            raise PrinterEditConflict('设备配置已变化，请重新打开编辑并确认')
        if 'ip' in changes:
            raise PrinterEditConflict('编辑设备时不能修改 IP')
        updated = {**existing, **changes}
        endpoint = printer_endpoint(updated)
        if endpoint != printer_endpoint(existing) and any(
            printer['id'] != printer_id and printer_endpoint(printer) == endpoint for printer in cfg['printers']
        ):
            raise PrinterEditConflict('该地址、端口和路径已被其他设备使用')
        if updated != existing:
            existing.update(changes)
            _save_printers(db, cfg)
        return editable_printer(existing)


def create_printer(printer_data):
    """Create only: existing IDs/endpoints must never be silently updated."""
    with _printer_transaction() as db:
        cfg = _printer_config(db)
        if any(printer_endpoint(printer) == printer_endpoint(printer_data) for printer in cfg['printers']):
            raise ValueError('该地址、端口和路径的打印机已存在，请使用现有设备')
        if any(printer['id'] == printer_data['id'] for printer in cfg['printers']):
            raise ValueError('设备标识已存在，未保存')
        cfg['printers'].append(dict(printer_data))
        _save_printers(db, cfg)
    return dict(printer_data)


def get_printers() -> List[Dict[str, Any]]:
    """获取所有已注册的打印机列表"""
    return load_config_strict()["printers"]


def get_default_printer_id() -> str:
    return load_config_strict()["system"]["default_printer_id"]


def set_default_printer(printer_id: str) -> Dict[str, Any]:
    """Save the default device in the same transaction as the catalog read."""
    with _printer_transaction() as db:
        cfg = _printer_config(db)
        printer = next((p for p in cfg['printers'] if p['id'] == printer_id), None)
        if printer is None:
            raise ValueError('未找到指定打印机，请选择已配置的设备')
        if cfg['system']['default_printer_id'] != printer_id:
            cfg['system']['default_printer_id'] = printer_id
            _save_printers(db, cfg)
        return printer


def get_printer(printer_id: Optional[str] = None) -> Dict[str, Any]:
    """Resolve an exact configured ID, or the configured default when omitted."""
    cfg = load_config_strict()
    target_id = printer_id or cfg['system']['default_printer_id']
    for printer in cfg['printers']:
        if printer['id'] == target_id:
            return printer
    raise ValueError(f'未找到指定打印机：{target_id}；请选择已配置的设备')


def add_or_update_printer(printer_data: Dict[str, Any]) -> Dict[str, Any]:
    """Legacy API retained with transactional writes; new UI uses create_printer."""
    if not printer_data.get('id'):
        raise ValueError('打印机必须包含唯一 id')
    with _printer_transaction() as db:
        cfg = _printer_config(db)
        existing = next((printer for printer in cfg['printers'] if printer['id'] == printer_data['id']), None)
        if existing is None:
            cfg['printers'].append(dict(printer_data))
        else:
            existing.update(printer_data)
        _save_printers(db, cfg)
    return printer_data


def get_server_config() -> Dict[str, Any]:
    cfg = load_config()
    return cfg.get("server", DEFAULT_CONFIG["server"])


def get_queue_config() -> Dict[str, Any]:
    cfg = load_config()
    return cfg.get("queue", DEFAULT_CONFIG["queue"])


def get_converter_config() -> Dict[str, Any]:
    """读取文档转换器配置；环境变量优先于 config.json。

    Returns:
        Dict[str, Any]: 包含 mode、gotenberg_url、timeout_sec、max_upload_mb 与可选 basic_auth 元组
    Example:
        >>> get_converter_config()['mode']
        'gotenberg'
    """
    cfg = {**DEFAULT_CONFIG["converter"], **load_config().get("converter", {})}
    cfg["gotenberg_url"] = os.environ.get("PRINT_HUB_GOTENBERG_URL", cfg["gotenberg_url"]).strip().rstrip("/")
    mode = os.environ.get("PRINT_HUB_CONVERTER_MODE", cfg["mode"]).strip().lower()
    if mode not in ("gotenberg", "local", "auto"):
        raise ValueError(f"不支持的转换模式: {mode}，仅支持 gotenberg / local / auto")
    cfg["mode"] = mode
    if os.environ.get("PRINT_HUB_GOTENBERG_TIMEOUT"):
        cfg["timeout_sec"] = int(os.environ["PRINT_HUB_GOTENBERG_TIMEOUT"])
    if os.environ.get("PRINT_HUB_MAX_UPLOAD_MB"):
        cfg["max_upload_mb"] = int(os.environ["PRINT_HUB_MAX_UPLOAD_MB"])
    user = os.environ.get("PRINT_HUB_GOTENBERG_USER")
    password = os.environ.get("PRINT_HUB_GOTENBERG_PASSWORD")
    cfg["basic_auth"] = (user, password) if user and password else None
    return cfg


def get_auth_config() -> Dict[str, Optional[str]]:
    """读取服务端可选鉴权令牌，仅从环境变量加载以防凭证硬编码。

    Returns:
        Dict[str, Optional[str]]: 包含 api_token 与 admin_token（未配置时为 None）
    Example:
        >>> sorted(get_auth_config().keys())
        ['admin_token', 'api_token']
    """
    api_token = os.environ.get("PRINT_HUB_API_TOKEN", "").strip() or None
    admin_token = os.environ.get("PRINT_HUB_ADMIN_TOKEN", "").strip() or None
    return {"api_token": api_token, "admin_token": admin_token}


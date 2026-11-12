# -*- coding: utf-8 -*-
"""
多打印机配置加载与管理模块
支持企业级多打印机注册、动态切换与默认路由
"""
import os
import json
import tempfile
import threading
import time
from pathlib import Path
from contextlib import contextmanager
from typing import List, Dict, Any, Optional

BASE_DIR = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
CONFIG_PATH = os.path.join(BASE_DIR, "config.json")

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


def load_config() -> Dict[str, Any]:
    if os.path.exists(CONFIG_PATH):
        try:
            with open(CONFIG_PATH, "r", encoding="utf-8") as f:
                return json.load(f)
        except Exception:
            pass
    return DEFAULT_CONFIG


_write_lock = threading.RLock()


@contextmanager
def config_lock():
    """Serialize read-modify-write across threads and Windows/macOS processes."""
    with _write_lock:
        with open(CONFIG_PATH + '.lock', 'a+b') as lock:
            lock.seek(0, os.SEEK_END)
            if lock.tell() == 0:
                lock.write(b'0')
                lock.flush()
            deadline = time.monotonic() + 3
            acquired = False
            try:
                while not acquired:
                    lock.seek(0)
                    try:
                        if os.name == 'nt':
                            import msvcrt
                            msvcrt.locking(lock.fileno(), msvcrt.LK_NBLCK, 1)
                        else:
                            import fcntl
                            fcntl.flock(lock.fileno(), fcntl.LOCK_EX | fcntl.LOCK_NB)
                        acquired = True
                    except OSError:
                        if time.monotonic() >= deadline:
                            raise ValueError('配置正在被其他进程修改，请稍后重试')
                        time.sleep(0.05)
                yield
            finally:
                if acquired:
                    lock.seek(0)
                    if os.name == 'nt':
                        import msvcrt
                        msvcrt.locking(lock.fileno(), msvcrt.LK_UNLCK, 1)
                    else:
                        import fcntl
                        fcntl.flock(lock.fileno(), fcntl.LOCK_UN)


def load_config_strict():
    """Validate configuration before selecting a device or saving changes."""
    try:
        with open(CONFIG_PATH, encoding='utf-8') as file:
            cfg = json.load(file)
    except (OSError, ValueError) as exc:
        raise ValueError('配置文件无法读取或已损坏；请检查 config.json') from exc
    if not isinstance(cfg, dict) or not isinstance(cfg.get('printers'), list) or not isinstance(cfg.get('system'), dict):
        raise ValueError('配置结构不完整；请检查 config.json')
    ids = [printer.get('id') for printer in cfg['printers'] if isinstance(printer, dict)]
    if len(ids) != len(cfg['printers']) or not all(isinstance(pid, str) and pid for pid in ids) or len(ids) != len(set(ids)):
        raise ValueError('配置中的设备标识无效或重复；请检查 config.json')
    if cfg['system'].get('default_printer_id') not in ids:
        raise ValueError('默认打印机配置无效；请重新选择默认设备')
    return cfg


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


def _save_config_locked(cfg):
    original = Path(CONFIG_PATH).read_bytes()
    _atomic_write(CONFIG_PATH + '.bak', original)
    _atomic_write(CONFIG_PATH, (json.dumps(cfg, ensure_ascii=False, indent=2) + '\n').encode('utf-8'))


def save_config(cfg: Dict[str, Any]):
    with config_lock():
        load_config_strict()
        _save_config_locked(cfg)


def printer_endpoint(printer):
    return (printer.get('ip'), int(printer.get('ipp_port', 631)), printer.get('ipp_path', '/ipp/print'))


def create_printer(printer_data):
    """Create only: existing IDs/endpoints must never be silently updated."""
    with config_lock():
        cfg = load_config_strict()
        if any(printer_endpoint(printer) == printer_endpoint(printer_data) for printer in cfg['printers']):
            raise ValueError('该地址、端口和路径的打印机已存在，请使用现有设备')
        if any(printer['id'] == printer_data['id'] for printer in cfg['printers']):
            raise ValueError('设备标识已存在，未保存')
        cfg['printers'].append(dict(printer_data))
        _save_config_locked(cfg)
    return dict(printer_data)


def get_printers() -> List[Dict[str, Any]]:
    """获取所有已注册的打印机列表"""
    return load_config_strict()["printers"]


def get_default_printer_id() -> str:
    return load_config_strict()["system"]["default_printer_id"]


def set_default_printer(printer_id: str) -> Dict[str, Any]:
    """Save an existing device as the default without overwriting concurrent edits."""
    with config_lock():
        cfg = load_config_strict()
        printer = next((p for p in cfg['printers'] if p['id'] == printer_id), None)
        if printer is None:
            raise ValueError('未找到指定打印机，请选择已配置的设备')
        if cfg['system']['default_printer_id'] != printer_id:
            cfg['system']['default_printer_id'] = printer_id
            _save_config_locked(cfg)
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
    """Legacy API retained, with locked atomic writes; new UI uses create_printer."""
    if not printer_data.get('id'):
        raise ValueError('打印机必须包含唯一 id')
    with config_lock():
        cfg = load_config_strict()
        existing = next((printer for printer in cfg['printers'] if printer['id'] == printer_data['id']), None)
        if existing is None:
            cfg['printers'].append(dict(printer_data))
        else:
            existing.update(printer_data)
        _save_config_locked(cfg)
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


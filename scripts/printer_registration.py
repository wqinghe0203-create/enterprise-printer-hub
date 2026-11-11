"""Read-only IPP probes and create-only registration, shared by Web and CLI."""
import ipaddress
import re
import socket
import threading
import time
import uuid

from .config import create_printer, load_config_strict, printer_endpoint
from .ipp_core import IPPClient

PRIVATE_NETWORKS = tuple(ipaddress.ip_network(value) for value in ('10.0.0.0/8', '172.16.0.0/12', '192.168.0.0/16'))
PROBE_TTL = 300


class RegistrationError(ValueError):
    def __init__(self, message, status=400):
        super().__init__(message)
        self.status = status


def is_local_address(address):
    try:
        # UDP connect determines the local route without sending a packet.
        with socket.socket(socket.AF_INET, socket.SOCK_DGRAM) as connection:
            connection.connect((address, 631))
            return connection.getsockname()[0] == address
    except OSError:
        return False


def validate_endpoint(data):
    if not isinstance(data, dict):
        raise RegistrationError('请提供打印机地址')
    if not isinstance(data.get('ip'), str):
        raise RegistrationError('请输入有效的公司私网 IPv4 地址')
    try:
        address = ipaddress.IPv4Address(data.get('ip', ''))
    except (ValueError, TypeError, ipaddress.AddressValueError) as exc:
        raise RegistrationError('请输入有效的公司私网 IPv4 地址') from exc
    if not any(address in network for network in PRIVATE_NETWORKS) or is_local_address(str(address)):
        raise RegistrationError('只支持公司私网打印机地址，不允许本机、公网、组播或保留地址')
    port = data.get('ipp_port', 631)
    try:
        if isinstance(port, bool) or isinstance(port, float):
            raise ValueError()
        port = int(port)
    except (ValueError, TypeError) as exc:
        raise RegistrationError('IPP 端口必须为 1–65535 的整数') from exc
    if not 1 <= port <= 65535:
        raise RegistrationError('IPP 端口必须为 1–65535 的整数')
    path = data.get('ipp_path', '/ipp/print')
    if not isinstance(path, str) or len(path) > 200 or not re.fullmatch(r'/[A-Za-z0-9/_.~-]*', path) or '..' in path or '//' in path:
        raise RegistrationError('IPP 路径必须以 / 开头，不能含查询参数、转义、跳转或特殊字符')
    return {'ip': str(address), 'ipp_port': port, 'ipp_path': path}


def as_list(value):
    return value if isinstance(value, list) else [value] if value is not None else []


def inspect_printer(endpoint):
    """Get-Printer-Attributes only. Never Validate/Print/Cancel a job here."""
    client = IPPClient(printer_cfg={**endpoint, 'name': '待添加打印机'}, follow_redirects=False)
    status = client.get_printer_status()
    if not status.get('online'):
        return {'status':'failed', 'compatible':False, 'can_save':False, 'error':'连接失败：' + status.get('state_desc', '未取得有效 IPP 响应')}
    capability = status.get('capabilities', {})
    formats = as_list(capability.get('document-format-supported'))
    types = as_list(capability.get('pwg-raster-document-type-supported'))
    resolutions = as_list(capability.get('pwg-raster-document-resolution-supported'))
    raster = 'image/pwg-raster' in formats and bool(set(types) & {'srgb_8','sgray_8'}) and any(
        isinstance(value, dict) and value.get('units') == 3 and value.get('x') == value.get('y') and 72 <= value.get('x', 0) <= 600 for value in resolutions)
    sides = as_list(capability.get('sides-supported', ['one-sided']))
    copies = capability.get('copies-supported', [1,1])
    copies_valid = isinstance(copies, list) and len(copies) == 2 and all(isinstance(value, int) for value in copies) and copies[0] <= 1 <= copies[1]
    usable_sides = [side for side in ('two-sided-long-edge','two-sided-short-edge','one-sided') if side in sides]
    if 'application/pdf' not in formats and capability.get('pwg-raster-document-sheet-back', 'normal') != 'normal':
        usable_sides = [side for side in usable_sides if side == 'one-sided']
    compatible = ('application/pdf' in formats or raster) and bool(usable_sides) and copies_valid
    default_duplex = {'two-sided-long-edge':'duplex_long','two-sided-short-edge':'duplex_short','one-sided':'simplex'}.get(usable_sides[0] if usable_sides else '', 'simplex')
    duplicate = any(printer_endpoint(printer) == printer_endpoint(endpoint) for printer in load_config_strict()['printers'])
    return {
        'status':'succeeded', 'compatible':compatible, 'can_save':compatible and not duplicate,
        'duplicate':duplicate,
        'error': '该设备已存在，请使用列表中的现有设备' if duplicate else '' if compatible else '连接成功，但设备未声明兼容的 PDF / PWG Raster 格式或可用打印设置，暂不支持接入',
        'default_duplex':default_duplex,
        'name':str(status.get('name') or status.get('model') or '网络打印机')[:80],
        'model':str(status.get('model') or 'Network Printer')[:120],
        'formats':formats, 'sides':sides, 'copies':copies,
        'color_supported': bool('srgb_8' in types or 'rgb_8' in types or capability.get('color-supported', False)),
        'duplex_supported':any(side in sides for side in ('two-sided-long-edge','two-sided-short-edge')),
        'reasons':status.get('reasons', []),
    }


def clean_text(value, label, maximum, required=False):
    if not isinstance(value, str):
        raise RegistrationError(label + '格式不正确')
    value = value.strip()
    if required and not value or len(value) > maximum or any(ord(char) < 32 or ord(char) == 127 for char in value):
        raise RegistrationError(label + f'不能为空或包含控制字符，长度最多 {maximum} 字' if required else label + f'长度最多 {maximum} 字，不能含控制字符')
    return value


def save_inspected_printer(endpoint, result, name, location):
    if result.get('status') != 'succeeded' or not result.get('compatible') or not result.get('can_save'):
        raise RegistrationError(result.get('error') or '请先完成设备检测', 409)
    printer = {
        **endpoint, 'id':'printer_' + uuid.uuid4().hex,
        'name':clean_text(name, '设备名称', 80, required=True),
        'location':clean_text(location, '位置', 120), 'model':clean_text(result['model'], '型号', 120),
        'color_supported':result['color_supported'], 'duplex_supported':result['duplex_supported'],
        'default_duplex':result.get('default_duplex', 'simplex'),
    }
    try:
        return create_printer(printer)
    except ValueError as exc:
        raise RegistrationError(str(exc), 409) from exc


class ProbeRegistry:
    def __init__(self):
        self._lock = threading.Lock()
        self._records = {}
        self._active = False

    def _prune(self):
        now = time.monotonic()
        self._records = {key:value for key,value in self._records.items() if value['expires'] > now}

    def start(self, data):
        endpoint = validate_endpoint(data)
        with self._lock:
            self._prune()
            if self._active:
                raise RegistrationError('已有设备正在检测，请稍后重试', 409)
            if len(self._records) >= 32:
                raise RegistrationError('检测次数过多，请稍后重试', 429)
            probe_id = uuid.uuid4().hex
            self._records[probe_id] = {'endpoint':endpoint, 'result':{'status':'pending'}, 'expires':time.monotonic()+PROBE_TTL}
            self._active = True
            threading.Thread(target=self._run, args=(probe_id, endpoint), daemon=True).start()
        return {'probe_id':probe_id, 'status':'pending'}

    def _run(self, probe_id, endpoint):
        try:
            result = inspect_printer(endpoint)
        except Exception:
            result = {'status':'failed','can_save':False,'error':'设备检测失败，请检查网络及本机配置文件后重试'}
        with self._lock:
            if probe_id in self._records:
                self._records[probe_id]['result'] = result
                self._records[probe_id]['expires'] = time.monotonic()+PROBE_TTL
            self._active = False

    def get(self, probe_id):
        with self._lock:
            self._prune()
            record = self._records.get(probe_id)
            if record is None:
                raise RegistrationError('检测结果已过期或不存在，请重新检测设备', 410)
            return {'probe_id':probe_id, **record['result'], 'endpoint':dict(record['endpoint'])}

    def save(self, data):
        if not isinstance(data.get('probe_id'), str) or not re.fullmatch(r'[0-9a-f]{32}', data['probe_id']):
            raise RegistrationError('检测任务标识无效，请重新检测设备')
        with self._lock:
            self._prune()
            record = self._records.get(data.get('probe_id'))
            if record is None:
                raise RegistrationError('检测结果已过期或不存在，请重新检测设备', 410)
            if record.get('saved'):
                return dict(record['saved']), False
            printer = save_inspected_printer(record['endpoint'], record['result'], data.get('name'), data.get('location', ''))
            record['saved'] = printer
            return dict(printer), True

"""Send Office files to the installation's HTTPS conversion service, with hybrid fallback support."""

import base64
from dataclasses import dataclass, field
import http.client
import json
from pathlib import Path
import socket
import ssl
from typing import Optional
from urllib.parse import urlsplit
import uuid
import zipfile


ROOT = Path(__file__).resolve().parents[1]
OFFICE_SUFFIXES = frozenset({'.doc', '.docx', '.xls', '.xlsx'})
COMPOUND_FILE_HEADER = bytes.fromhex('D0 CF 11 E0 A1 B1 1A E1')
MAX_SOURCE_BYTES = 25 * 1024 * 1024
MAX_PDF_BYTES = 100 * 1024 * 1024
CONNECT_TIMEOUT = 8
CONVERSION_TIMEOUT = 105
PDF_HINT = '可先导出为 PDF 再打印。'


class OfficeConversionError(RuntimeError):
    def __init__(self, message, *, missing=False, status=None, retryable=False):
        super().__init__(f'{message}。{PDF_HINT}')
        self.missing = missing
        self.status = status
        self.retryable = retryable


@dataclass(frozen=True)
class Settings:
    host: str
    port: int
    route: str
    authorization: str = field(repr=False)
    ca_file: Optional[Path] = None
    fallback_to_local: bool = False


def load_settings() -> Settings:
    path = ROOT / 'data' / 'office-converter.json'
    try:
        values = json.loads(path.read_text(encoding='utf-8-sig'))
        url, user, password = (values[key] for key in ('url', 'username', 'password'))
        if not all(isinstance(value, str) and value.isascii() and value.isprintable() and value.strip()
                   for value in (url, user, password)) or ':' in user:
            raise ValueError
        endpoint = urlsplit(url)
        port = endpoint.port or 443
        if (endpoint.scheme != 'https' or not endpoint.hostname or '@' in endpoint.netloc
                or '?' in url or '#' in url or ' ' in url or '\\' in url or endpoint.port == 0):
            raise ValueError
        auth = base64.b64encode(f'{user}:{password}'.encode('ascii')).decode('ascii')
        ca_file = values.get('ca_file')
        if ca_file is not None and (not isinstance(ca_file, str) or not ca_file.strip() or '\x00' in ca_file):
            raise ValueError
        fallback_to_local = bool(values.get('fallback_to_local', False))
        return Settings(endpoint.hostname, port, endpoint.path.rstrip('/') + '/forms/libreoffice/convert',
                        'Basic ' + auth, path.parent / ca_file if ca_file else None, fallback_to_local)
    except FileNotFoundError:
        raise OfficeConversionError('未配置 Office 转换服务，请联系管理员', missing=True, retryable=True) from None
    except (OSError, ValueError, KeyError, TypeError):
        raise OfficeConversionError('Office 转换配置无效，请管理员检查 data/office-converter.json', retryable=False) from None


def is_remote_configured() -> bool:
    """Return True if remote microservice configuration file exists and is structurally valid."""
    try:
        load_settings()
        return True
    except OfficeConversionError:
        return False


def check_source_size(size: int):
    if size <= 0:
        raise OfficeConversionError('Office 文件为空')
    if size > MAX_SOURCE_BYTES:
        raise OfficeConversionError('Office 文件超过 25 MiB 转换上限')


def check_source_format(stream, suffix: str, size: int):
    header = stream.read(8)
    if suffix in ('.docx', '.xlsx'):
        if header == COMPOUND_FILE_HEADER:
            raise OfficeConversionError('Office 文件可能受密码保护，或其格式与扩展名不符')
        try:
            with zipfile.ZipFile(stream) as package:
                package.getinfo('[Content_Types].xml')
                package.getinfo('_rels/.rels')
        except (zipfile.BadZipFile, UnicodeDecodeError, KeyError):
            raise OfficeConversionError('Office 文件已损坏或格式与扩展名不符') from None
    elif not (size >= 512 and header == COMPOUND_FILE_HEADER
              or suffix == '.doc' and header.startswith(b'{\\rtf')
              or suffix == '.xls' and size >= 8 and header[:2] in (b'\x09\x00', b'\x09\x02', b'\x09\x04', b'\x09\x08')):
        raise OfficeConversionError('Office 文件已损坏或格式与扩展名不符')
    stream.seek(0)


def submission_error(path, size: Optional[int] = None) -> Optional[str]:
    """Pre-flight check for file submission: validates format, size, and converter availability."""
    suffix = Path(path).suffix.lower()
    if suffix not in OFFICE_SUFFIXES:
        return None

    if size is not None:
        try:
            check_source_size(size)
        except OfficeConversionError as exc:
            return str(exc)

    try:
        load_settings()
        return None
    except OfficeConversionError as exc:
        if exc.missing:
            # File unconfigured: check local fallback capability
            try:
                from .office_local import detect_local_engines
                local_status = detect_local_engines()
                kind = 'word' if suffix in ('.doc', '.docx') else 'excel'
                if local_status.get(kind, False):
                    if size is not None:
                        check_source_size(size)
                    return None
            except Exception:
                pass
        return str(exc)


def response_error(status: int) -> OfficeConversionError:
    if status in (401, 403):
        return OfficeConversionError('Office 转换服务拒绝访问，请管理员检查凭据和内网访问权限', status=status, retryable=False)
    if status == 413:
        return OfficeConversionError('Office 文件超过服务端上传上限', status=status, retryable=False)
    if status == 429:
        return OfficeConversionError('Office 转换服务繁忙，请稍后重新提交', status=status, retryable=True)
    if status in (408, 504):
        return OfficeConversionError('Office 转换请求超时', status=status, retryable=True)
    if status in (400, 415, 422):
        return OfficeConversionError('Office 转换失败，请检查文件格式、文件是否损坏或加密', status=status, retryable=False)
    if status == 503:
        return OfficeConversionError('Office 转换服务处理超时或暂不可用，请联系管理员', status=status, retryable=True)
    if status == 502:
        return OfficeConversionError('Office 转换服务暂不可用，请联系管理员', status=status, retryable=True)
    if status == 500:
        return OfficeConversionError('Office 转换服务处理失败，请检查文件或联系管理员', status=status, retryable=True)
    return OfficeConversionError(f'Office 转换服务返回异常状态（HTTP {status}），请管理员检查服务地址', status=status, retryable=True)


def save_response(response, destination: Path):
    media_type = response.getheader('Content-Type', '').split(';', 1)[0].strip().lower()
    if media_type != 'application/pdf' or response.read(5) != b'%PDF-':
        raise OfficeConversionError('Office 转换服务未返回有效的 PDF', retryable=False)
    length = response.getheader('Content-Length')
    if length is not None:
        if len(length) > 10 or not length.isascii() or not length.isdecimal():
            raise OfficeConversionError('Office 转换服务返回的 PDF 长度无效', retryable=False)
        if int(length) > MAX_PDF_BYTES:
            raise OfficeConversionError('转换后的 PDF 超过 100 MiB', retryable=False)

    received = 0
    try:
        with destination.open('wb') as output:
            chunk = b'%PDF-'
            while chunk:
                received += len(chunk)
                if received > MAX_PDF_BYTES:
                    raise OfficeConversionError('转换后的 PDF 超过 100 MiB', retryable=False)
                output.write(chunk)
                try:
                    chunk = response.read(64 * 1024)
                except OSError as exc:
                    if isinstance(exc, (TimeoutError, socket.timeout)):
                        raise OfficeConversionError('Office 转换请求超时', retryable=True) from None
                    raise OfficeConversionError('Office 转换连接中断，请检查内网或 VPN', retryable=True) from None
    except OSError:
        raise OfficeConversionError('无法保存转换后的 PDF，请检查本机目录权限和剩余空间', retryable=False) from None
    if length is not None and int(length) != received:
        raise OfficeConversionError('Office 转换服务返回的 PDF 下载不完整', retryable=False)


def download_pdf(source: Path, destination: Path, *, settings: Optional[Settings] = None):
    """Stream a single multipart upload and bounded PDF response without following redirects."""
    settings = settings or load_settings()
    suffix = source.suffix.lower()
    if suffix not in OFFICE_SUFFIXES:
        raise OfficeConversionError('Office 文件格式不受支持', retryable=False)
    try:
        context = ssl.create_default_context()
        if settings.ca_file is not None:
            context.load_verify_locations(cafile=str(settings.ca_file))
    except (OSError, ValueError):
        raise OfficeConversionError('无法加载 Office 转换服务的 CA 证书，请联系管理员', retryable=True) from None

    boundary = uuid.uuid4().hex
    prefix = (f'--{boundary}\r\nContent-Disposition: form-data; name="files"; '
              f'filename="document{suffix}"\r\nContent-Type: application/octet-stream\r\n\r\n').encode('ascii')
    ending = f'\r\n--{boundary}--\r\n'.encode('ascii')
    with source.open('rb') as stream:
        size = stream.seek(0, 2)
        check_source_size(size)
        stream.seek(0)
        check_source_format(stream, suffix, size)

        def body():
            yield prefix
            remaining = size
            while remaining:
                chunk = stream.read(min(remaining, 64 * 1024))
                if not chunk:
                    raise OfficeConversionError('上传期间 Office 源文件发生变化，请重新选择文件', retryable=False)
                remaining -= len(chunk)
                yield chunk
            yield ending

        conn = http.client.HTTPSConnection(settings.host, settings.port, timeout=CONNECT_TIMEOUT, context=context)
        try:
            conn.request('POST', settings.route, body=body(), headers={
                'Authorization': settings.authorization,
                'Accept': 'application/pdf',
                'Content-Type': f'multipart/form-data; boundary={boundary}',
                'Content-Length': str(len(prefix) + size + len(ending)),
            })
            conn.sock.settimeout(CONVERSION_TIMEOUT)
            with conn.getresponse() as response:
                if response.status != 200:
                    raise response_error(response.status)
                save_response(response, destination)
        except ssl.SSLCertVerificationError:
            raise OfficeConversionError('Office 转换服务 HTTPS 证书验证失败，请联系管理员', retryable=True) from None
        except (TimeoutError, socket.timeout):
            raise OfficeConversionError('Office 转换请求超时', retryable=True) from None
        except (OSError, http.client.HTTPException):
            raise OfficeConversionError('Office 转换连接中断或无法建立，请检查内网或 VPN', retryable=True) from None
        finally:
            conn.close()

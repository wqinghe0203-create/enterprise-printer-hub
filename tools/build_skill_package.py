"""Build a clean WorkBuddy import ZIP without changing runtime source or settings.

Only delivery files are allowlisted. Environments, logs, uploads, queues, VCS and
caches must never be copied from a developer's working directory into a release.
"""
import ast
import hashlib
import io
import json
import re
import zipfile
from pathlib import Path


ROOT = Path(__file__).resolve().parents[1]
NAME = 'enterprise-printer-hub'
FIXED_FILES = (
    'SKILL.md', 'README.md', 'manifest.json', 'requirements.txt', 'config.json',
    '安装依赖.bat', '启动网页控制台.bat', '启动局域网控制台.bat',
    'WorkBuddy使用与排查.md', '导入安装说明.md',
    'templates/index.html', 'templates/office-check.docx', 'templates/office-check.xlsx',
    'scripts/bootstrap-windows.ps1', 'scripts/bootstrap-mac.sh',
    'deploy/office-converter/README.md', 'deploy/office-converter/compose.yaml',
    'deploy/office-converter/ingress.conf', 'deploy/office-converter/nginx.conf.example',
)


def main():
    version = json.loads((ROOT / 'manifest.json').read_text(encoding='utf-8'))['version']
    skill = (ROOT / 'SKILL.md').read_text(encoding='utf-8')
    skill_version = re.search(r'(?m)^\s+version:\s*"([^"]+)"\s*$', skill)
    if not re.fullmatch(r'\d+\.\d+\.\d+', version) or not skill_version or skill_version[1] != version:
        raise ValueError('manifest.json 与 SKILL.md 的交付版本必须一致')
    if f'版本：{version}。' not in (ROOT / 'README.md').read_text(encoding='utf-8'):
        raise ValueError('README 当前交付版本不一致')
    archive_name = f'{NAME}-{version}-import.zip'
    if archive_name not in (ROOT / '导入安装说明.md').read_text(encoding='utf-8'):
        raise ValueError('导入说明中的压缩包版本不一致')
    paths = sorted(set(FIXED_FILES) | {
        str(file.relative_to(ROOT).as_posix())
        for file in (ROOT / 'scripts').iterdir()
        if file.is_file() and file.suffix in ('.py', '.bat')
    })
    payload = {}
    for relative in paths:
        file = ROOT / relative
        if file.is_symlink() or not file.resolve().is_relative_to(ROOT):
            raise ValueError('Refusing external source: ' + relative)
        data = file.read_bytes()
        if file.suffix == '.py':
            ast.parse(data.decode('utf-8-sig'), filename=relative)
        payload[relative] = data
    output = ROOT / 'dist'
    output.mkdir(exist_ok=True)
    archive = output / archive_name
    buffer = io.BytesIO()
    with zipfile.ZipFile(buffer, 'w', compression=zipfile.ZIP_DEFLATED, compresslevel=9) as zip_file:
        for relative, data in payload.items():
            zip_file.writestr(f'{NAME}/{relative}', data)
    if buffer.getbuffer().nbytes >= 3_000_000:
        raise ValueError('WorkBuddy ZIP 必须小于 3,000,000 字节')
    with zipfile.ZipFile(buffer) as zip_file:
        if zip_file.testzip() is not None:
            raise RuntimeError('ZIP CRC validation failed')
        if set(zip_file.namelist()) != {f'{NAME}/{p}' for p in payload}:
            raise RuntimeError('Unexpected file in ZIP')
        for relative, data in payload.items():
            if zip_file.read(f'{NAME}/{relative}') != data or (ROOT / relative).read_bytes() != data:
                raise RuntimeError('File changed while packaging: ' + relative)
    archive.write_bytes(buffer.getbuffer())
    manifest = {
        'archive': archive.name,
        'archive_sha256': hashlib.sha256(buffer.getbuffer()).hexdigest(),
        'files': {p: {'size': len(data), 'sha256': hashlib.sha256(data).hexdigest()}
                  for p, data in payload.items()},
    }
    checksum_file = archive.with_suffix('.sha256.json')
    checksum_file.write_text(json.dumps(manifest, ensure_ascii=False, indent=2) + '\n', encoding='utf-8')
    print(json.dumps({'archive': str(archive), 'compressed_bytes': archive.stat().st_size,
                      'uncompressed_bytes': sum(item['size'] for item in manifest['files'].values()),
                      'files': len(manifest['files']), 'checksum': str(checksum_file),
                      'validation': 'CRC, Python syntax and byte-for-byte source comparison passed'},
                     ensure_ascii=False))


if __name__ == '__main__':
    main()

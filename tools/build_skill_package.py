"""Build a release that preserves the server's databases and runtime config."""
import hashlib
import json
import os
from pathlib import Path
import re
import tempfile
from zipfile import ZIP_DEFLATED, ZipFile


ROOT = Path(__file__).resolve().parents[1]
ROOT_FILES = (
    'SKILL.md', 'manifest.json', 'requirements.txt', 'README.md',
    '导入安装说明.md', 'WorkBuddy使用与排查.md',
    '安装依赖.bat', '启动网页控制台.bat', '启动局域网控制台.bat', '发布打包.bat',
    'remote.example.json',
)


def package_files(root):
    files = {name: (root / name).read_bytes() for name in ROOT_FILES}
    for directory, patterns in (
        ('scripts', ('*.py', '*.ps1', '*.sh', '*.bat')),
        ('tools', ('build_skill_package.py',)),
        ('templates', ('index.html', 'office-check.docx', 'office-check.xlsx')),
        ('deploy', ('*.yml', '*.service', '*.example', '*.bat')),
    ):
        for pattern in patterns:
            for path in sorted((root / directory).rglob(pattern)):
                if path.is_symlink():
                    raise ValueError(f'发布文件不能使用符号链接：{path}')
                files[path.relative_to(root).as_posix()] = path.read_bytes()
    source = root / 'config.json'
    if not source.exists():
        source = root / 'config.example.json'
    files['config.example.json'] = source.read_bytes()
    for name, content in files.items():
        if name.endswith('.py'):
            compile(content, name, 'exec')
    manifest = json.loads(files['manifest.json'])
    version = re.search(r'^  version: "([^"]+)"$', files['SKILL.md'].decode('utf-8').replace('\r\n', '\n'), re.MULTILINE)
    if not version or version[1] != manifest['version']:
        raise ValueError('SKILL.md 和 manifest.json 版本不一致')
    return files, manifest


def build_package(root=ROOT, destination=None):
    root = Path(root)
    files, manifest = package_files(root)
    destination = Path(destination) if destination else root / 'dist'
    destination.mkdir(parents=True, exist_ok=True)
    name = manifest['name']
    package = destination / f'{name}-{manifest["version"]}-import.zip'
    temporary = None
    try:
        with tempfile.NamedTemporaryFile(dir=destination, suffix='.zip', delete=False) as output:
            temporary = Path(output.name)
        with ZipFile(temporary, 'w', compression=ZIP_DEFLATED) as archive:
            for path, content in sorted(files.items()):
                archive.writestr(f'{name}/{path}', content)
        with ZipFile(temporary) as archive:
            if archive.testzip() is not None:
                raise ValueError('ZIP 完整性校验失败')
            if set(archive.namelist()) != {f'{name}/{path}' for path in files}:
                raise ValueError('ZIP 文件白名单校验失败')
            for path, content in files.items():
                if archive.read(f'{name}/{path}') != content:
                    raise ValueError(f'ZIP 内容不一致：{path}')
        os.replace(temporary, package)
    finally:
        if temporary and temporary.exists():
            temporary.unlink()
    hashes = {path: hashlib.sha256(content).hexdigest() for path, content in sorted(files.items())}
    checksum = {'package': package.name, 'sha256': hashlib.sha256(package.read_bytes()).hexdigest(), 'files': hashes}
    package.with_suffix('.sha256.json').write_text(json.dumps(checksum, ensure_ascii=False, indent=2) + '\n', encoding='utf-8')
    return package


if __name__ == '__main__':
    print(build_package())

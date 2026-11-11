import errno
from pathlib import Path


def ensure_directory(path):
    path = Path(path)
    if not path.is_dir():
        try:
            path.mkdir(parents=True, exist_ok=True)
        except OSError as error:
            if error.errno != errno.EEXIST or not path.is_dir():
                raise OSError(f'无法使用目录 {path}：{error.strerror or error}') from error
    return path

"""Native LibreOffice conversion endpoint for the Print Hub HTTPS client."""

import os
from pathlib import Path
import shutil
import signal
import subprocess
import tempfile
import threading

from flask import Flask, Response, abort, request

from scripts.office_remote import (
    MAX_PDF_BYTES, MAX_SOURCE_BYTES, OFFICE_SUFFIXES, OfficeConversionError, check_source_format,
)


CONVERSION_TIMEOUT = 90
PDF_FILTERS = {'.doc': 'writer_pdf_Export', '.docx': 'writer_pdf_Export',
               '.xls': 'calc_pdf_Export', '.xlsx': 'calc_pdf_Export'}
app = Flask(__name__)
app.config.update(MAX_CONTENT_LENGTH=MAX_SOURCE_BYTES + 1024, MAX_FORM_PARTS=1)
# Run exactly one Gunicorn worker. A second thread can reject concurrent requests.
conversion_slot = threading.Lock()


def convert(source, work):
    profile = work / 'profile'
    user_profile = profile / 'user'
    user_profile.mkdir(parents=True)
    (user_profile / 'registrymodifications.xcu').write_text('''<?xml version="1.0" encoding="UTF-8"?>
<oor:items xmlns:oor="http://openoffice.org/2001/registry">
  <item oor:path="/org.openoffice.Office.Common/Security/Scripting">
    <prop oor:name="DisableMacrosExecution" oor:op="fuse"><value>true</value></prop>
    <prop oor:name="DisableActiveContent" oor:op="fuse"><value>true</value></prop>
    <prop oor:name="BlockUntrustedRefererLinks" oor:op="fuse"><value>true</value></prop>
  </item>
</oor:items>
''', encoding='utf-8')
    executable = os.environ.get('OFFICE_SOFFICE', '/usr/bin/soffice')
    command = [executable, '-env:UserInstallation=' + profile.as_uri(), '--headless',
               '--convert-to', 'pdf:' + PDF_FILTERS[source.suffix], '--outdir', str(work), str(source)]
    env = {'PATH': os.defpath, 'LANG': 'C.UTF-8', 'HOME': str(work), 'TMPDIR': str(work)}
    with subprocess.Popen(command, cwd=work, env=env, stdin=subprocess.DEVNULL,
                          stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL,
                          start_new_session=True) as process:
        try:
            status = process.wait(timeout=CONVERSION_TIMEOUT)
        except subprocess.TimeoutExpired:
            try:
                os.killpg(process.pid, signal.SIGKILL)
            except ProcessLookupError:
                pass
            process.wait()
            raise

    output = source.with_suffix('.pdf')
    if status or not output.is_file():
        abort(422)
    return output


@app.post('/office-converter/forms/libreoffice/convert')
def convert_request():
    if request.mimetype != 'multipart/form-data':
        abort(415)
    if not conversion_slot.acquire(blocking=False):
        abort(429)

    try:
        uploads = request.files.getlist('files')
        if len(uploads) != 1 or len(request.files) != 1 or request.form:
            abort(400)
        upload = uploads[0]
        suffix = Path(upload.filename or '').suffix.lower()
        if suffix not in OFFICE_SUFFIXES:
            abort(415)
        size = upload.stream.seek(0, os.SEEK_END)
        if size > MAX_SOURCE_BYTES:
            abort(413)
        if not size:
            abort(422)
        upload.stream.seek(0)
        try:
            check_source_format(upload.stream, suffix, size)
        except OfficeConversionError:
            abort(422)

        with tempfile.TemporaryDirectory(prefix='print-hub-office-') as folder:
            work = Path(folder)
            source = work / f'document{suffix}'
            with source.open('wb') as destination:
                shutil.copyfileobj(upload.stream, destination)
            try:
                pdf = convert(source, work)
            except subprocess.TimeoutExpired:
                abort(504)
            except OSError:
                app.logger.error('LibreOffice could not be started')
                abort(503)
            with pdf.open('rb') as stream:
                content = stream.read(MAX_PDF_BYTES + 1)
            if len(content) > MAX_PDF_BYTES or not content.startswith(b'%PDF-'):
                abort(422)
        return Response(content, mimetype='application/pdf', headers={'Cache-Control': 'no-store'})
    finally:
        conversion_slot.release()

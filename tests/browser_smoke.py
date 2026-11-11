"""Optional UI regression: pip install playwright; uses installed Microsoft Edge.
All device I/O is mocked, files and jobs are stored in a temporary directory.
"""
import sys
import tempfile
import threading
from pathlib import Path
from unittest.mock import patch

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from playwright.sync_api import sync_playwright, expect
from scripts import queue_scheduler
from scripts.printer_service import PrinterService


def main():
    with tempfile.TemporaryDirectory() as directory, patch.object(queue_scheduler, 'STATE_FILE', str(Path(directory)/'queue.json')):
        from scripts import web_server
        service = PrinterService()
        pid = queue_scheduler.get_default_printer_id()
        for n in range(45):
            job = service.scheduler.enqueue('report.pdf', 'Alice' if n % 2 else 'Bob', filename=f'report-{n}.pdf')
            service.scheduler.cancel_job(job['id'])
        with patch.object(web_server, 'svc', service), patch.object(web_server, 'UPLOAD_DIR', directory), patch('scripts.printer_service.IPPClient') as ipp:
            ipp.return_value.get_printer_status.return_value = {'online':False, 'state_desc':'模拟离线'}
            ipp.return_value.get_jobs.return_value = []
            ipp.return_value.get_job.return_value = {'success':False}
            server = web_server.ThreadingHTTPServer(('127.0.0.1',0), web_server.PrinterWebHandler)
            thread = threading.Thread(target=server.serve_forever, daemon=True)
            thread.start()
            try:
                with sync_playwright() as playwright:
                    browser = playwright.chromium.launch(channel='msedge', headless=True)
                    page = browser.new_page(viewport={'width':1440,'height':900})
                    errors=[]
                    page.on('pageerror',lambda error:errors.append(str(error)))
                    page.goto(f'http://127.0.0.1:{server.server_port}/#history')
                    expect(page.locator('#history-pagination')).to_contain_text('共 45 条')
                    page.locator('#history-pagination button[data-direction="1"]').click()
                    expect(page.locator('#history-pagination')).to_contain_text('第 2 / 9 页')
                    page.locator('#history-tbody details').first.locator('summary').click()
                    original=page.locator('#history-tbody').inner_text()
                    job=service.scheduler.enqueue('new.pdf','Alice')
                    service.scheduler.cancel_job(job['id'])
                    page.evaluate('fetchHistory()')
                    expect(page.locator('#history-updates')).to_be_visible()
                    assert page.locator('#history-tbody').inner_text()==original
                    assert page.locator('#history-tbody details').first.get_attribute('open') is not None
                    page.locator('#history-updates').click()
                    expect(page.locator('#history-pagination')).to_contain_text('共 46 条')
                    page.locator('#history-filters [name="user"]').fill('Alice')
                    page.locator('#history-filters button[type="submit"]').click()
                    expect(page.locator('#history-pagination')).to_contain_text('共 23 条')
                    page.locator('#history-filters [name="search"]').fill('#101')
                    page.locator('#history-filters button[type="submit"]').click()
                    expect(page.locator('#history-tbody')).to_contain_text('没有符合条件')
                    page.locator('#history-filters button[type="reset"]').click()
                    expect(page.locator('#history-pagination')).to_contain_text('共 46 条')
                    page.locator('#history-filters [name="scope"]').select_option('device')
                    expect(page.locator('#history-note')).to_contain_text('仅展示设备当前可查询')
                    expect(page.locator('#history-filters [name="start"]')).to_be_disabled()
                    page.locator('#history-filters button[type="reset"]').click()
                    expect(page.locator('#history-filters [name="start"]')).to_be_enabled()
                    # Emulate response loss after server has durably accepted the upload.
                    page.evaluate("routeTo('new-print')")
                    page.locator('#user-name').fill('回归用户')
                    with patch.object(web_server, 'submission_error', return_value='转换暂未就绪'), \
                            patch.object(web_server.office_check, 'status') as office, \
                            patch.object(web_server.office_check, 'start_check', side_effect=lambda **_: office.return_value) as recheck:
                        office.return_value = {'enabled':True, 'state':'checking', 'checking_format':'excel', 'elapsed_seconds':4,
                                               'message':'转换暂未就绪', 'word':True, 'excel':False}
                        page.evaluate('officePolling=true; refreshOfficeProgress()')
                        expect(page.locator('#office-recheck')).to_have_text('检测中')
                        expect(page.locator('#office-message')).to_contain_text('正在检查 Excel（已等待 4 秒）')
                        page.locator('#copies-input').fill('3')
                        page.locator('#file-input').set_input_files({'name':'book.xlsx','buffer':b'fixture','mimeType':'application/octet-stream'})
                        page.locator('#submit-btn').click()
                        expect(page.locator('#toast')).to_contain_text('转换暂未就绪')
                        expect(page.locator('#file-label')).to_contain_text('book.xlsx')
                        assert page.evaluate("sessionStorage.getItem('print-request')") is None
                        assert service.scheduler.get_jobs()==[]
                        page.evaluate("""() => {
                            const originalFetch = window.fetch;
                            window.fetch = (url, options) => {
                                if (url !== '/api/office-status' || options.method === 'POST') {
                                    return originalFetch(url, options);
                                }
                                return new Promise((resolve, reject) => {
                                    options.signal.addEventListener('abort', () => reject(options.signal.reason), {once:true});
                                });
                            };
                        }""")
                        page.evaluate('void refreshOfficeProgress()')
                        expect(page.locator('#office-stage')).to_contain_text('无法读取检查状态', timeout=8000)
                        expect(page.locator('#office-recheck')).to_be_enabled()
                        office.return_value = {'enabled':True, 'state':'partial', 'message':'远程转换检查完成。',
                                               'word':False, 'excel':True}
                        page.locator('#office-recheck').click()
                        expect(page.locator('#office-stage')).to_contain_text('部分可用')
                        expect(page.locator('#office-message')).to_contain_text('远程转换检查完成')
                        expect(page.locator('#office-recheck')).to_have_text('重新检测')
                        expect(page.locator('#file-label')).to_contain_text('book.xlsx')
                        expect(page.locator('#copies-input')).to_have_value('3')
                        expect(page.locator('#user-name')).to_have_value('回归用户')
                        recheck.assert_called_once_with(background=True)
                        page.evaluate('officePolling=false')
                    page.locator('#copies-input').fill('3')
                    page.locator('#file-input').set_input_files({'name':'example.pdf','mimeType':'application/pdf','buffer':b'%PDF-mocked'})
                    def lose_response(route):
                        response=route.fetch()
                        assert response.status==202
                        route.abort()
                    page.route('**/api/upload_print',lose_response,times=1)
                    page.locator('#submit-btn').click()
                    expect(page.locator('#submit-btn')).to_have_text('核查并重试本次提交')
                    expect(page.locator('#file-actions button').last).to_be_disabled()
                    expect(page.locator('#print-summary')).to_contain_text('本次重试仍使用原设置')
                    before=service.scheduler.get_jobs()
                    assert len(before)==1
                    assert before[0]['copies']==3
                    page.locator('#submit-btn').click()
                    expect(page.locator('#submission-card')).to_contain_text('example.pdf')
                    assert len(service.scheduler.get_jobs())==1
                    expect(page.locator('#submit-btn')).to_have_text('提交打印任务')
                    job=before[0]
                    service.scheduler.record_delivery(job['id'],'unknown',error_msg='模拟发送结果未知')
                    page.locator('#verify-task').click()
                    expect(page.locator('#submission-card')).to_contain_text('勿重复提交')
                    page.reload()
                    expect(page.locator('#submission-card')).to_contain_text('example.pdf')
                    # A request that never reached the server remains retryable after reload,
                    # using its original ID, file and settings.
                    page.evaluate("routeTo('overview')")
                    page.locator('#user-name').fill('回归用户')
                    page.locator('#print-more-settings summary').click()
                    page.locator('#copies-input').fill('3')
                    page.locator('#print-more-settings summary').click()
                    page.locator('#file-input').set_input_files({'name':'retry.pdf','mimeType':'application/pdf','buffer':b'%PDF-retry'})
                    page.route('**/api/upload_print', lambda route:route.abort(), times=1)
                    page.locator('#submit-btn').click()
                    expect(page.locator('#submit-btn')).to_have_text('核查并重试本次提交')
                    expect(page.locator('#file-actions button').last).to_be_disabled()
                    expect(page.locator('#print-summary')).to_contain_text('本次重试仍使用原设置')
                    request_id=page.evaluate("sessionStorage.getItem('print-request')")
                    page.reload()
                    expect(page.locator('#print-summary')).to_contain_text('3 份')
                    page.locator('#user-name').fill('回归用户')
                    page.locator('#file-input').set_input_files({'name':'retry.pdf','mimeType':'application/pdf','buffer':b'%PDF-retry'})
                    page.locator('#submit-btn').click()
                    expect(page.locator('#submission-card')).to_contain_text('retry.pdf')
                    expect(page.locator('#submit-btn')).to_have_text('提交打印任务')
                    assert service.scheduler.get_request(request_id)['filename']=='retry.pdf'
                    assert service.scheduler.get_request(request_id)['copies']==3
                    assert len(service.scheduler.get_jobs())==2
                    # A different file can be submitted while the older request remains available for lookup.
                    page.locator('#file-input').set_input_files({'name':'old.pdf','buffer':b'%PDF-old','mimeType':'application/pdf'})
                    page.route('**/api/upload_print', lambda route:route.abort(), times=1)
                    page.locator('#submit-btn').click()
                    expect(page.locator('#submit-btn')).to_have_text('核查并重试本次提交')
                    old_request=page.evaluate("sessionStorage.getItem('print-request')")
                    page.reload()
                    page.locator('#user-name').fill('回归用户')
                    page.locator('#file-input').set_input_files({'name':'new.pdf','buffer':b'%PDF-new','mimeType':'application/pdf'})
                    page.locator('#submit-btn').click()
                    expect(page.locator('#toast')).to_contain_text('开始新任务')
                    page.get_by_role('button', name='开始新任务').click()
                    expect(page.locator('#file-label')).to_contain_text('new.pdf')
                    expect(page.locator('#submit-btn')).to_have_text('提交打印任务')
                    page.locator('#previous-submissions summary').click()
                    expect(page.locator('#previous-submission-list')).to_contain_text('old.pdf')
                    page.locator('#previous-submission-list button').click()
                    expect(page.locator('#previous-submission-list')).to_contain_text('暂未找到这笔提交')
                    page.locator('#submit-btn').click()
                    expect(page.locator('#submission-card')).to_contain_text('new.pdf')
                    assert service.scheduler.get_request(old_request) is None
                    assert len(service.scheduler.get_jobs())==3
                    page.set_viewport_size({'width':390,'height':844})
                    page.evaluate("routeTo('history')")
                    expect(page.locator('#history')).to_be_visible()
                    assert page.evaluate('document.documentElement.scrollWidth <= window.innerWidth')
                    output=Path('.cache/browser');output.mkdir(parents=True,exist_ok=True)
                    page.screenshot(path=str(output/'history-mobile.png'),full_page=True)
                    page.set_viewport_size({'width':1440,'height':900})
                    page.screenshot(path=str(output/'history-desktop.png'),full_page=True)
                    assert not errors, errors
                    browser.close()
                    print('Browser smoke passed: history, filters, updates, upload recovery, task status, mobile layout.')
            finally:
                server.shutdown()
                server.server_close()
                thread.join(5)


if __name__=='__main__':
    main()

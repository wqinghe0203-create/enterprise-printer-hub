"""Overview records and standalone form regressions; all APIs are mocked.

Run: python tests/browser_overview.py (Playwright and local Edge required).
No worker, printer connection, dependency installation or runtime data changes.
"""
import threading
from functools import partial
from http.server import SimpleHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from urllib.parse import parse_qs, urlsplit

from playwright.sync_api import expect, sync_playwright


class QuietHandler(SimpleHTTPRequestHandler):
    def log_message(self, *args):
        pass


def main():
    root = Path(__file__).resolve().parents[1]
    server = ThreadingHTTPServer(('127.0.0.1', 0), partial(QuietHandler, directory=str(root/'templates')))
    thread = threading.Thread(target=server.serve_forever, daemon=True)
    thread.start()
    devices = [dict(id=pid, name=name, location=location, online=online, queued_count=0)
               for pid, name, location, online in [('a', '总部打印机', '27F 总部', True),
                                                   ('b', '工作区打印机', '28楼工作区', False)]]

    def record(pid, index):
        return dict(id=index, filename=f'{pid}-会议资料-{index}.pdf', printer_id=pid,
                    user='张三', pages=2, copies=1, created_at='2026-11-03 11:35:00', status='completed')

    rows = {'a': [record('a', n) for n in range(6, 0, -1)], 'b': [record('b', 50)]}
    rows['a'][0]['filename'] = '用于验证长名称省略与悬停查看完整名称的部门季度会议打印资料' * 5 + '.pdf'
    failures = []
    held = []
    hold_next = []
    recent_requests = []

    def api(route):
        url = urlsplit(route.request.url)
        params = parse_qs(url.query)
        pid = params.get('printer_id', ['a'])[0]
        if url.path == '/api/office-status':
            result = {'enabled':False}
        elif url.path == '/api/dashboard':
            device = next(d for d in devices if d['id'] == pid)
            result = dict(cluster=devices, selected_printer_id=pid,
                          printer={**device, 'state_desc':'空闲就绪' if device['online'] else '离线',
                                   'model':'模拟设备', 'reasons':[]},
                          virtual_queue=[], hw_active_jobs=[], active_printing=None,
                          history={'hw_history':[]})
        elif url.path == '/api/history':
            if 'scope' not in params:
                recent_requests.append(params)
                if hold_next:
                    hold_next.pop()
                    held.append(route)
                    return
                if failures:
                    failures.pop()
                    route.fulfill(status=503, json={'error':'模拟记录读取失败'})
                    return
            result = dict(success=True, items=rows[pid][:5], total=len(rows[pid]),
                          page=1, pages=1, size=5)
        else:
            raise AssertionError('Unexpected API call: ' + route.request.url)
        route.fulfill(json=result)

    try:
        with sync_playwright() as playwright:
            browser = playwright.chromium.launch(channel='msedge', headless=True)
            page = browser.new_page(viewport={'width':1440, 'height':900})
            errors = []
            page.on('pageerror', lambda error: errors.append(str(error)))
            page.route('**/api/**', api)
            page.goto(f'http://127.0.0.1:{server.server_port}/index.html#overview')
            expect(page.locator('#recent-records-tbody tr')).to_have_count(5)
            # Drive refreshes explicitly so timing cannot mask single-flight bugs.
            page.evaluate('clearInterval(0); for(let id=1;id<100;id++)clearInterval(id)')
            assert all(p['page']==['1'] and p['size']==['5'] for p in recent_requests)
            long_name = page.locator('#recent-records-tbody .doc-name').first
            assert long_name.get_attribute('title') == rows['a'][0]['filename']
            assert long_name.evaluate('e=>e.scrollWidth>e.clientWidth')
            assert 56 <= page.locator('#recent-records-tbody tr').first.bounding_box()['height'] <= 64
            assert page.locator('#recent-records').bounding_box()['y'] > page.locator('#queue').bounding_box()['y']

            # New data waits for the reader to apply it, including an empty list.
            original = page.locator('#recent-records-tbody').inner_text()
            rows['a'] = [record('a', 99)]
            page.evaluate('fetchRecentRecords()')
            expect(page.locator('#recent-records-updates')).to_be_visible()
            assert page.locator('#recent-records-tbody').inner_text() == original
            page.locator('#recent-records-updates').click()
            expect(page.locator('#recent-records-tbody')).to_contain_text('a-会议资料-99.pdf')
            rows['a'] = []
            page.evaluate('fetchRecentRecords()')
            expect(page.locator('#recent-records-updates')).to_be_visible()
            page.locator('#recent-records-updates').click()
            expect(page.locator('#recent-records-tbody')).to_contain_text('暂无本机打印记录')

            # The complete history's filters and page state are independent.
            page.evaluate("filters.search='保留筛选';paginationState.history.page=3")
            page.locator('#recent-records a').click()
            expect(page.locator('#recent-records')).to_be_hidden()
            assert page.evaluate('filters.search') == '保留筛选'
            page.evaluate("routeTo('overview')")
            expect(page.locator('#recent-records')).to_be_visible()
            assert page.evaluate('filters.search') == '保留筛选'

            # Offline device still exposes its local history; failed reads can retry.
            failures.append(True)
            page.locator('#printer-select').select_option('b')
            expect(page.locator('#recent-records-error')).to_be_visible()
            expect(page.locator('#recent-records-tbody')).not_to_contain_text('a-会议资料')
            page.locator('#recent-records-error button').click()
            expect(page.locator('#recent-records-tbody')).to_contain_text('b-会议资料-50.pdf')
            expect(page.locator('#recent-records-error')).to_be_hidden()
            expect(page.locator('#p-status-text')).to_have_text('离线')

            # Repeated refreshes share one in-flight request.
            hold_next.append(True)
            count = len(recent_requests)
            page.evaluate('fetchRecentRecords();fetchRecentRecords();fetchRecentRecords()')
            page.wait_for_function('recentBusy')
            page.wait_for_timeout(50)
            assert len(recent_requests) == count + 1
            assert len(held) == 1
            held.pop().fulfill(json=dict(items=rows['b']))
            page.wait_for_function('!recentBusy')

            # A late result from the old device must never replace the new one,
            # even if an underlying request ignores its AbortSignal.
            page.evaluate("""() => {
                window.originalRequestJSON=requestJSON;
                requestJSON=(url,options)=>url.startsWith('/api/history?') && !url.includes('scope=') && url.includes('printer_id=b')
                    ? new Promise(resolve=>window.releaseOldRecent=resolve)
                    : window.originalRequestJSON(url,options);
                fetchRecentRecords();
            }""")
            rows['a'] = [record('a', 100)]
            page.locator('#printer-select').select_option('a')
            expect(page.locator('#recent-records-tbody')).not_to_contain_text('b-会议资料')
            page.evaluate("releaseOldRecent({items:[{filename:'迟到的旧设备记录'}]})")
            expect(page.locator('#recent-records-tbody')).to_contain_text('a-会议资料-100.pdf')
            expect(page.locator('#recent-records-tbody')).not_to_contain_text('迟到的旧设备记录')
            page.evaluate('() => { requestJSON=window.originalRequestJSON; }')

            # One form is reused, with preserved data and unchanged sidebar layout.
            page.locator('#file-input').set_input_files(dict(name='会议资料.pdf', mimeType='application/pdf', buffer=b'%PDF-fixture'))
            page.locator('#user-name').fill('张三')
            page.locator('#duplex-select').select_option('simplex')
            page.locator('#print-more-settings summary').click()
            page.locator('#prio-select').select_option('80')
            page.locator('#copies-input').fill('3')
            page.locator('#print-more-settings summary').click()
            expect(page.locator('#priority-summary')).to_contain_text('3 份')
            sidebar_width = page.locator('.upload-column').bounding_box()['width']
            output = root/'.cache/browser'
            output.mkdir(parents=True, exist_ok=True)
            page.screenshot(path=str(output/'overview-recent-records.png'), full_page=True)
            for width, height in [(1920,1080), (1440,900), (1024,900), (390,844)]:
                page.set_viewport_size(dict(width=width, height=height))
                page.evaluate("routeTo('new-print')")
                expect(page.locator('.main-column')).to_be_hidden()
                expect(page.locator('.upload-column')).to_be_visible()
                grid = page.locator('.workspace-grid').bounding_box()
                card = page.locator('#new-print').bounding_box()
                assert abs(grid['width'] - card['width']) < 2
                assert abs(grid['x'] - card['x']) < 2
                assert page.locator('#upload-form').count() == 1
                assert page.locator('#file-input').count() == 1
                assert page.locator('#new-print').evaluate('e=>e.scrollHeight<=e.clientHeight')
                assert page.evaluate('document.documentElement.scrollWidth<=innerWidth')
                expect(page.locator('#file-label')).to_contain_text('会议资料.pdf')
                expect(page.locator('#user-name')).to_have_value('张三')
                expect(page.locator('#duplex-select')).to_have_value('simplex')
                expect(page.locator('#prio-select')).to_have_value('80')
                expect(page.locator('#copies-input')).to_be_visible()
                expect(page.locator('#copies-input')).to_have_value('3')
                expect(page.locator('#print-more-settings summary')).to_be_hidden()
                assert page.locator('#print-more-settings').get_attribute('open') is not None
                if width > 1050:
                    file_area = page.locator('.print-file-area').bounding_box()
                    settings_area = page.locator('.print-settings-area').bounding_box()
                    assert abs(file_area['height'] - settings_area['height']) < 2
                    assert abs(page.locator('#drop-zone').bounding_box()['height'] - settings_area['height']) < 2
                    assert abs(page.locator('#submit-btn').bounding_box()['width'] - 240) < 2
                    assert page.locator('.print-file-area').bounding_box()['x'] < page.locator('.print-settings-area').bounding_box()['x']
                else:
                    assert page.locator('.print-file-area').bounding_box()['y'] < page.locator('.print-settings-area').bounding_box()['y']
                    if width == 390:
                        assert page.locator('#submit-btn').bounding_box()['height'] >= 44
                page.screenshot(path=str(output/f'new-print-full-{width}.png'), full_page=True)
                page.evaluate("routeTo('overview')")
                expect(page.locator('#recent-records')).to_be_visible()
                assert page.evaluate('document.documentElement.scrollWidth<=innerWidth')
                if width == 1440:
                    assert abs(page.locator('.upload-column').bounding_box()['width'] - sidebar_width) < 2
                    expect(page.locator('#copies-input')).to_be_hidden()
            assert not errors, errors
            browser.close()
            print('Overview/full-width form passed: records, refresh, error retry, offline, stale responses, single-flight, filter isolation, form persistence, 1920/1440/1024/390. No print calls.')
    finally:
        server.shutdown()
        server.server_close()
        thread.join(5)


if __name__ == '__main__':
    main()

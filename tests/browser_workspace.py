"""Compact UI regression against fixture APIs. No application worker or device I/O.
Run: python tests/browser_workspace.py (requires playwright and installed Edge).
"""
import json
import threading
from email.parser import BytesParser
from email import policy
from functools import partial
from http.server import SimpleHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from urllib.parse import urlsplit, parse_qs

from playwright.sync_api import sync_playwright, expect


class QuietHandler(SimpleHTTPRequestHandler):
    def log_message(self, *args):
        pass


def main():
    root = Path(__file__).resolve().parents[1]
    server = ThreadingHTTPServer(('127.0.0.1', 0), partial(QuietHandler, directory=str(root/'templates')))
    thread = threading.Thread(target=server.serve_forever, daemon=True)
    thread.start()
    devices = [dict(id=f'floor{i}', name=f'{i}楼打印机', location=f'{i}F 工作区', ip=f'10.0.0.{i}',
                    online=i!=28, queued_count=8 if i==27 else 0) for i in range(27, 35)]
    devices[2]['name'] = '研发与财务共享的超长名称彩色网络打印设备，用于检查窄窗口换行'
    devices[2]['location'] = '跨部门共享文印工作区 A 座办公楼'
    tasks = {}
    uploads = []
    history_requests = []
    fail_next = []
    devices[0]['is_default'] = True

    def dashboard(pid):
        device = next((p for p in devices if p['id']==pid), devices[0])
        jobs = [dict(id=n+101, printer_id=device['id'], printer_name=device['name'], filename=f"{device['id']}-任务{n}.pdf",
                     user='测试用户', pages=2, copies=1, priority=50, status='queued') for n in range(device['queued_count'])]
        jobs += [j for j in tasks.values() if j['printer_id']==device['id'] and j['status'] in ('queued','submitted','printing','unknown')]
        return dict(cluster=devices, selected_printer_id=device['id'], printer={**device, 'model':'模拟型号', 'gateway':'',
                    'state_desc':'空闲就绪' if device['online'] else '离线', 'reasons':['wifi-not-configured-report'] if device['online'] else []},
                    virtual_queue=jobs, hw_active_jobs=[], active_printing=None, history={'hw_history':[]}, printer_management_allowed=True)

    def api(route):
        url=urlsplit(route.request.url)
        params=parse_qs(url.query)
        if url.path=='/api/office-status':
            result={'enabled':False}
        elif url.path=='/api/dashboard':
            if fail_next:
                fail_next.pop()
                route.fulfill(status=503,json={'error':'模拟加载失败'})
                return
            pid=params.get('printer_id',[next(p['id'] for p in devices if p['is_default'])])[0]
            if not any(p['id']==pid for p in devices):
                route.fulfill(status=400,json={'success':False,'error':'未找到指定打印机：'+pid})
                return
            result=dashboard(pid)
        elif url.path=='/api/cluster':
            result=devices
        elif url.path=='/api/default-printer':
            assert route.request.method=='POST'
            pid=params['printer_id'][0]
            for device in devices:
                device['is_default']=device['id']==pid
            result={'success':True,'default_printer_id':pid}
        elif url.path=='/api/history':
            # The overview also reads this endpoint; date validation concerns
            # only requests from the full history form (which includes scope).
            if 'scope' in params:
                history_requests.append(params)
            pid=params.get('printer_id',['floor27'])[0]
            result=dict(success=True,items=[dict(id=900, filename=f'{pid}-历史.pdf', user='历史用户',
                        printer_name=pid, status='canceled', created_at='2026-11-02 10:00:00', copies=1, pages=2)],
                        total=1,pages=1,page=1,size=int(params.get('size',['5'])[0]))
            if params.get('search',[''])[0].startswith('#'):
                job=tasks.get(params['search'][0][1:])
                result['items']=[job] if job and job['printer_id']==pid and job['status']=='completed' else []
                result['total']=len(result['items'])
        elif url.path=='/api/task':
            result={'success':True,'job':tasks.get(params.get('job_id',[''])[0])}
        elif url.path=='/api/upload_print':
            message=BytesParser(policy=policy.default).parsebytes(('Content-Type: '+route.request.headers['content-type']+'\r\n\r\n').encode()+route.request.post_data_buffer)
            fields={part.get_param('name',header='content-disposition'):part for part in message.iter_parts()}
            pid=fields['printer'].get_payload(decode=True).decode()
            uploads.append(pid)
            job=dict(id=777, filename=fields['file'].get_filename(), printer_id=pid,
                     printer_name=next(p['name'] for p in devices if p['id']==pid),status='submitted')
            tasks['777']=job
            result={'success':True,'job':job}
        else:
            raise AssertionError('Unexpected API call: '+url.path)
        route.fulfill(json=result)

    try:
        with sync_playwright() as playwright:
            browser=playwright.chromium.launch(channel='msedge',headless=True)
            page=browser.new_page(viewport={'width':1440,'height':900})
            errors=[]
            page.on('pageerror',lambda e:errors.append(str(e)))
            page.route('**/api/**',api)
            # Retain a late response even after abort, exercising generation protection.
            page.add_init_script("""const realFetch=window.fetch;window.fetch=async (...args)=>{
                const response=await realFetch(...args);
                if(String(args[0]).includes('/api/dashboard') && window.fixtureDelay){const delay=window.fixtureDelay;window.fixtureDelay=0;await new Promise(resolve=>setTimeout(resolve,delay));}
                return response;
            };""")
            url=f'http://127.0.0.1:{server.server_port}'
            page.goto(url)
            expect(page.locator('#printer-select')).to_have_value('floor27')
            expect(page.locator('#user-name')).to_have_value('')
            expect(page.locator('#remember-user')).not_to_be_checked()
            page.locator('#user-name').fill('临时使用者')
            page.reload()
            expect(page.locator('#user-name')).to_have_value('')
            page.locator('#user-name').fill('已同意保存的姓名')
            page.locator('#remember-user').check()
            page.reload()
            expect(page.locator('#user-name')).to_have_value('已同意保存的姓名')
            page.locator('#clear-user').click()
            expect(page.locator('#user-name')).to_have_value('')
            assert page.evaluate("localStorage.getItem('print-hub-user')") is None
            page.locator('#user-name').fill('切换保留姓名')
            expect(page.locator('#queue-tbody tr')).to_have_count(5)
            expect(page.locator('#cluster-container')).to_be_hidden()
            assert page.locator('#queue').bounding_box()['y']<340
            assert page.locator('#submit-btn').bounding_box()['y']+page.locator('#submit-btn').bounding_box()['height']<900
            assert page.locator('.upload-card').evaluate('e=>e.scrollHeight<=e.clientHeight')
            for width in (1024,768):
                page.set_viewport_size({'width':width,'height':900})
                assert page.evaluate('document.documentElement.scrollWidth<=innerWidth')
            page.set_viewport_size({'width':1440,'height':900})
            expect(page.locator('#device-advisories')).to_be_visible()
            expect(page.locator('#device-advisory-text')).to_be_hidden()
            expect(page.locator('#device-warnings')).to_be_hidden()
            page.locator('#queue-view-all').click()
            expect(page.locator('#queue-pagination')).to_be_visible()
            page.locator('#queue-pagination select').select_option('10')
            expect(page.locator('#queue-tbody tr')).to_have_count(8)
            page.evaluate("routeTo('overview')")
            expect(page.locator('#queue-tbody tr')).to_have_count(5)
            page.locator('#file-input').set_input_files({'name':'switch.pdf','mimeType':'application/pdf','buffer':b'%PDF-fixture'})
            expect(page.locator('#file-actions')).to_be_visible()
            page.locator('#file-actions button').last.click()
            expect(page.locator('#file-label')).to_contain_text('选择文件')
            assert page.locator('#file-input').evaluate('e=>e.files.length')==0
            expect(page.locator('#file-actions')).to_be_hidden()
            with page.expect_file_chooser() as chooser:
                page.locator('#drop-zone').click()
            chooser.value.set_files({'name':'draft.pdf','mimeType':'application/pdf','buffer':b'%PDF-draft'})
            with page.expect_file_chooser() as chooser:
                page.locator('#file-actions button').first.click()
            chooser.value.set_files({'name':'switch.pdf','mimeType':'application/pdf','buffer':b'%PDF-fixture'})
            expect(page.locator('#file-label')).to_contain_text('switch.pdf')
            page.locator('#user-name').fill('切换保留姓名')
            page.locator('#duplex-select').select_option('simplex')
            page.locator('#print-more-settings summary').click()
            page.locator('#prio-select').select_option('80')
            page.locator('#print-more-settings summary').click()
            expect(page.locator('#print-summary')).to_contain_text('单面 · 1 份 · 高优先级')
            page.evaluate('fixtureDelay=650')
            page.locator('#printer-select').select_option('floor28')
            expect(page.locator('#device-selection-status')).to_contain_text('正在切换')
            expect(page.locator('#queue-data')).to_be_hidden()
            expect(page.locator('#submit-btn')).to_be_disabled()
            expect(page.locator('#p-status-text')).to_have_text('离线')
            expect(page.locator('#printer-alert-box')).to_be_visible()
            expect(page.locator('#upload-target-name')).to_contain_text('28楼打印机')
            expect(page.locator('#user-name')).to_have_value('切换保留姓名')
            expect(page.locator('#duplex-select')).to_have_value('simplex')
            expect(page.locator('#prio-select')).to_have_value('80')
            expect(page.locator('#file-label')).to_contain_text('switch.pdf')
            # Rapid A -> B -> A, with the first response deliberately delayed.
            page.evaluate("fixtureDelay=500;switchPrinter('floor27')")
            page.wait_for_timeout(50)
            page.evaluate("switchPrinter('floor28');switchPrinter('floor27')")
            expect(page.locator('#upload-target-name')).to_contain_text('27楼打印机')
            expect(page.locator('#queue-tbody')).to_contain_text('floor27-任务')
            expect(page.locator('#device-selection-status')).to_be_hidden()
            page.evaluate("routeTo('devices')")
            expect(page.locator('.device-row')).to_have_count(8)
            page.locator('[data-printer-id="floor28"]').click()
            expect(page.locator('#p-status-text')).to_have_text('离线')
            page.evaluate("routeTo('history')")
            expect(page.locator('#history-tbody')).to_contain_text('floor28-历史.pdf')
            page.locator('#printer-select').select_option('floor27')
            expect(page.locator('#history-tbody')).to_contain_text('floor27-历史.pdf')
            page.evaluate("routeTo('overview')")
            page.locator('#submit-btn').click()
            expect(page.locator('#submission-card')).to_contain_text('27楼打印机')
            assert uploads==['floor27']
            page.locator('#printer-select').select_option('floor28')
            expect(page.locator('#p-status-text')).to_have_text('离线')
            expect(page.locator('#submission-card')).to_contain_text('27楼打印机')
            page.locator('#file-input').set_input_files({'name':'保留草稿.pdf','mimeType':'application/pdf','buffer':b'%PDF-draft'})
            page.evaluate('paginationState.queue.size=5')
            page.locator('#submission-card details summary').click()
            page.locator('#submission-card button',has_text='查看队列').click()
            expect(page.locator('#printer-select')).to_have_value('floor27')
            expect(page.locator('#queue-tbody tr.job-highlight')).to_contain_text('#777')
            expect(page.locator('#queue-pagination')).to_contain_text('第 2 / 2 页')
            expect(page.locator('#file-label')).to_contain_text('保留草稿.pdf')
            expect(page.locator('#user-name')).to_have_value('切换保留姓名')
            tasks['777'].update(status='completed',pages=1,copies=1,created_at='2026-11-03 10:00:00')
            page.locator('#printer-select').select_option('floor28')
            expect(page.locator('#p-status-text')).to_have_text('离线')
            page.locator('#submission-card button',has_text='查看记录').click()
            expect(page.locator('#printer-select')).to_have_value('floor27')
            expect(page.locator('#history-tbody tr.job-highlight')).to_contain_text('#777')
            expect(page.locator('#history-filters [name="search"]')).to_have_value('#777')
            expect(page.locator('#history-filter-summary')).to_contain_text('#777')
            assert uploads==['floor27']
            page.locator('#history-filter-summary button').click()
            expect(page.locator('#history-filter-summary')).to_be_hidden()
            page.locator('#history-filters [name="start"]').fill('2026-11-03')
            page.locator('#history-filters [name="end"]').fill('2026-11-01')
            expect(page.locator('#history-filter-error')).to_contain_text('不能早于')
            page.locator('#history-filters button[type="submit"]').click()
            expect(page.locator('#history-filter-summary')).to_be_hidden()
            assert not any(p.get('start')==['2026-11-03'] and p.get('end')==['2026-11-01']
                           for p in history_requests)
            page.locator('#history-filters [name="end"]').fill('2026-11-05')
            page.locator('#history-filters button[type="submit"]').click()
            expect(page.locator('#history-filter-summary')).to_contain_text('2026-11-05')
            page.locator('#history-filter-summary button').click()
            expect(page.locator('#history-filter-error')).to_be_hidden()
            page.locator('#printer-select').select_option('floor28')
            expect(page.locator('#p-status-text')).to_have_text('离线')
            page.reload()
            expect(page.locator('#printer-select')).to_have_value('floor27')
            page.evaluate("routeTo('devices')")
            page.locator('[data-default-printer-id="floor28"]').click()
            expect(page.locator('#printer-select option[value="floor28"]')).to_contain_text('默认')
            expect(page.locator('#printer-select')).to_have_value('floor27')
            page.reload()
            expect(page.locator('#printer-select')).to_have_value('floor28')
            expect(page.locator('#p-status-text')).to_have_text('离线')
            # A device removed during the current page session requires explicit reselection.
            page.evaluate("activePrinterId='removed-device';fetchDashboard()")
            expect(page.locator('#device-selection-status')).to_contain_text('已不存在')
            expect(page.locator('#printer-select')).to_have_value('')
            expect(page.locator('#submit-btn')).to_be_disabled()
            page.locator('#printer-select').select_option('floor27')
            expect(page.locator('#device-selection-status')).to_be_hidden()
            fail_next.append(True)
            page.evaluate('fetchDashboard()')
            expect(page.locator('#device-selection-status')).to_contain_text('读取失败')
            expect(page.locator('#submit-btn')).to_be_disabled()
            page.evaluate('fetchDashboard()')
            expect(page.locator('#device-selection-status')).to_be_hidden()
            # Important warnings stay expanded; harmless connectivity notes are collapsed.
            warning=dashboard('floor27')
            warning['printer']['reasons']=['media-empty-warning','wifi-not-configured-report']
            page.evaluate('data=>renderDashboard(data)',warning)
            expect(page.locator('#device-warnings')).to_contain_text('缺纸')
            page.evaluate('data=>renderDashboard(data)',dashboard('floor27'))
            output=root/'.cache/browser';output.mkdir(parents=True,exist_ok=True)
            page.locator('#file-input').set_input_files({'name':'季度财务报表.pdf','mimeType':'application/pdf','buffer':b'%PDF-fixture'})
            page.screenshot(path=str(output/'workspace-desktop.png'),full_page=True)
            page.evaluate("routeTo('devices');switchPrinter('floor29')")
            expect(page.locator('#device-selection-status')).to_be_hidden()
            page.set_viewport_size({'width':390,'height':844})
            page.evaluate("routeTo('overview');switchPrinter('floor27')")
            expect(page.locator('#device-selection-status')).to_be_hidden()
            expect(page.locator('#queue-tbody tr')).to_have_count(5)
            assert page.evaluate('document.documentElement.scrollWidth<=innerWidth')
            page.evaluate("routeTo('devices');switchPrinter('floor29')")
            expect(page.locator('#device-selection-status')).to_be_hidden()
            assert page.evaluate('document.documentElement.scrollWidth<=innerWidth')
            page.screenshot(path=str(output/'workspace-mobile-devices.png'),full_page=True)
            page.evaluate("routeTo('new-print')")
            expect(page.locator('#file-label')).to_contain_text('季度财务报表.pdf')
            expect(page.locator('.upload-column')).to_be_visible()
            assert page.evaluate('document.documentElement.scrollWidth<=innerWidth')
            page.screenshot(path=str(output/'workspace-mobile-print.png'),full_page=True)
            assert not errors,errors
            browser.close()
            print('Compact workspace passed: device linkage, task location, file controls, opt-in identity, summary, filters, retries and mobile.')
    finally:
        server.shutdown();server.server_close();thread.join(5)


if __name__=='__main__':
    main()

"""Browser interaction test; run with a Python environment containing Playwright."""
import json
import socket
import subprocess
import time
from pathlib import Path
from playwright.sync_api import expect, sync_playwright

root = Path(__file__).resolve().parents[1]
with socket.socket() as sock:
    sock.bind(('127.0.0.1', 0))
    port = sock.getsockname()[1]
server = subprocess.Popen(['node', str(root / 'node_modules/vite/bin/vite.js'), '--host', '127.0.0.1', '--port', str(port), '--strictPort'], cwd=root, stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
try:
    with sync_playwright() as p:
        browser = p.chromium.launch()
        page = browser.new_page()
        submitted = []
        downloads = []
        stale = [False]
        def handle(route):
            path = route.request.url.split('/api/v1/', 1)[-1].split('?', 1)[0]
            if path == 'health':
                data = {'status':'ok','archive_root_configured':True,'cloud_model_configured':False,'asr_model_configured':False,'local_asr_device':'cpu','worker_count':1}
            elif path == 'platforms': data = []
            elif path == 'local-asr/models':
                if stale[0]:
                    route.fulfill(status=200, content_type='text/html', body='<html>old server</html>')
                    return
                data = {'installed':True,'model_root':'F:/models','models':[{'id':'small','ready':True,'job':None},{'id':'tiny','ready':False,'job':None}]}
            elif path == 'local-asr/models/preview': data = {'model':'tiny','revision':'a'*40,'size_bytes':1048576,'directory':'F:/models/tiny'}
            elif path == 'local-asr/models/prepare': downloads.append(route.request.post_data_json); data = {'started':True}
            elif path == 'config': data = {'settings':{'archive_root':None,'obsidian_reference_root':None,'obsidian_output_root':None,'model_provider':'zhipu','model_id':'glm-5.3-flash','asr_model_id':'glm-asr-2512','model_base_url':None,'vector_search_enabled':False,'local_asr_device':'auto'},'model_base_url_default':None,'model_providers':[],'directory_conflicts':[],'credential':{'configured':False,'available':True}}
            elif path == 'diagnostics': data = []
            elif path == 'sources/preview': data = {'platform':'bilibili','platform_name':'B站','source_id':'BV123','is_short':False,'requires_resolution':False,'message':'可提交'}
            elif path == 'tasks' and route.request.method == 'POST':
                submitted.append(route.request.post_data_json)
                data = {'task':{'id':'task1','source':{'kind':'url'},'status':'queued'},'asset':{}}
            else: data = {}
            route.fulfill(status=200, content_type='application/json', body=json.dumps(data, ensure_ascii=False))
        page.route('**/api/v1/**', handle)
        for attempt in range(60):
            try:
                page.goto(f'http://127.0.0.1:{port}/')
                break
            except Exception:
                if attempt == 59: raise
                time.sleep(.2)
        page.get_by_role('button', name='语音转文字设置').click()
        page.get_by_label('本次识别方式').select_option('local')
        expect(page.get_by_text('本地模型已准备，可以选择转写。')).to_be_visible()
        expect(page.get_by_label('本次模型').locator('option', has_text='均衡版（small）')).to_have_count(1)
        expect(page.get_by_text('适合多数中文口播，可先用它测试效果', exact=False)).to_be_visible()
        page.get_by_role('button', name='确认并关闭').last.click()
        page.locator('#step-transcribe').check()
        expect(page.locator('#step-transcribe-help')).to_contain_text('本地 faster-whisper')
        page.locator('textarea').first.fill('https://www.bilibili.com/video/BV1234567890')
        page.get_by_role('button', name='创建任务 · 查看后续流程').click()
        page.wait_for_timeout(500)
        assert submitted and submitted[0]['transcription_mode'] == 'local'
        assert submitted[0]['local_model'] == 'small' and submitted[0]['local_device'] == 'cpu'
        assert submitted[0]['consents'] == []
        page.goto(f'http://127.0.0.1:{port}/settings?tab=models')
        expect(page.locator('#local-model-prepare').locator('option', has_text='均衡版（small）')).to_have_count(1)
        expect(page.get_by_text('适合多数中文口播，可先用它测试效果', exact=False)).to_be_visible()
        page.locator('#local-model-prepare').select_option('tiny')
        expect(page.get_by_text('适合快速试用，但容易遗漏或误识别', exact=False)).to_be_visible()
        page.get_by_role('button', name='查看下载信息').click()
        expect(page.get_by_text('下载大小约', exact=False)).to_be_visible()
        assert not downloads
        page.get_by_role('button', name='确认下载').click()
        expect(page.get_by_role('button', name='查看下载信息')).to_be_enabled()
        assert downloads[0]['model'] == 'tiny' and downloads[0]['confirmed'] is True
        stale[0] = True
        page.goto(f'http://127.0.0.1:{port}/')
        page.get_by_role('button', name='语音转文字设置').click()
        page.get_by_label('本次识别方式').select_option('local')
        expect(page.get_by_text('本机服务版本不匹配或模型状态数据无效，请重启工作台。')).to_be_visible()
        expect(page.get_by_label('本次模型')).to_be_disabled()
        stale[0] = False
        page.get_by_role('button', name='重试读取模型').click()
        expect(page.get_by_label('本次模型')).to_be_enabled()
        expect(page.get_by_text('本地模型已准备，可以选择转写。')).to_be_visible()
        browser.close()
        print('Local model selection and task payload passed')
finally:
    server.terminate()
    server.wait(timeout=10)

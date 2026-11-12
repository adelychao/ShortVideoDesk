"""Exercise the task artifact list with an isolated, mocked workbench API."""
import json
import os
import re
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
now = '2026-10-29T12:00:00Z'
def artifact(id, kind, *, mime='text/markdown', version=1, available=True, metadata=None):
    return dict(id=id, task_id='t', step_id='s', kind=kind, name=id+'.md', relative_path='folder/'+id+'.md', sha256='a'*64, version=version, metadata=metadata or {}, created_at=f'2026-10-29T12:00:{version:02d}Z', mime_type=mime, file_url=f'/api/v1/artifacts/{id}/file', file_available=available)

items = [artifact('video', 'video', mime='video/mp4'), artifact('cover', 'cover', mime='image/jpeg'),
         artifact('notes', 'video_notes'), artifact('transcript-old', 'transcript'), artifact('transcript-new', 'transcript', version=2),
         artifact('ocr', 'ocr_result', available=False), artifact('comment', 'note', metadata={'scope':'comments'}),
         artifact('script-story', 'script', metadata={'style':'故事口语'}), artifact('script-edu', 'script', metadata={'style':'科普讲解'}),
         artifact('script-fun', 'script', metadata={'style':'轻松幽默'}), artifact('card', 'knowledge_card', metadata={'title':'观点'}), artifact('card-two', 'knowledge_card', version=2, metadata={'title':'案例'}),
         artifact('hidden', 'transcript_raw'), artifact('other', 'other_document')]
step = dict(id='s', kind='transcribe', status='completed', depends_on=[], input_artifact_ids=[], output_artifact_ids=[], progress=100, message='', retry_count=0, ordinal=0)
task = dict(id='t', asset_id='a', status='completed', created_at=now, updated_at=now, steps=[step], source={'kind':'url'})
detail = dict(task=task, asset=dict(id='a', title='测试任务', source_kind='url', platform='douyin', source_id='123', archive_relative_path='folder', archived_at=now), artifacts=items, consents=[], model_runs=[])
opens = []
old_server = [False]
card_contents = {}
try:
    with sync_playwright() as p:
        browser = p.chromium.launch()
        page = browser.new_page(viewport={'width':1440,'height':900})
        page.set_default_timeout(5000)
        errors = []
        page.on('pageerror', lambda error: errors.append(str(error)))
        def handle(route):
            path = route.request.url.split('/api/v1/', 1)[-1].split('?', 1)[0]
            if old_server[0] and path == 'artifacts/comment/text':
                route.fulfill(status=422, content_type='application/json', body=json.dumps({'detail':'unsupported'})); return
            if old_server[0] and path == 'artifacts/comment/file':
                route.fulfill(status=200, content_type='text/markdown', body='# 本次采集评论\n\n已保存的评论'); return
            if path == 'tasks/t': data = detail
            elif path.endswith('/open'):
                opens.append(route.request.url); data = {'opened':True}
            elif path.endswith('/text') and route.request.method == 'PUT':
                source_id = path.split('/')[1]
                revision_id = source_id + '-revision'
                revision = artifact(revision_id, 'knowledge_card_revision', version=3, metadata={'title':'观点','human_edited':True})
                items.append(revision)
                card_contents[revision_id] = json.loads(route.request.post_data or '{}').get('content', '')
                data = revision
            elif path.endswith('/text'):
                id = path.split('/')[1]
                data = {'artifact_id':id,'sha256':'a'*64,'content':card_contents.get(id, '# 文本内容 '+id+'\n\n**重点**\n\n<script>window.testXss=1</script>')}
            elif path == 'config': data = {'settings':{'model_provider':'zhipu','model_id':'glm'},'credential':{'available':True,'configured':True},'model_providers':[],'directory_conflicts':[]}
            else: data = {}
            route.fulfill(status=200, content_type='application/json', body=json.dumps(data, ensure_ascii=False))
        page.route('**/api/v1/**', handle)
        page.route('**/api/health', lambda route: route.fulfill(status=200, content_type='application/json', body='{"restart_required":false}'))
        for attempt in range(50):
            try:
                page.goto(f'http://127.0.0.1:{port}/task/t')
                break
            except Exception:
                if attempt == 49: raise
                time.sleep(.2)
        expect(page.locator('[data-artifact-group="media"]')).to_be_visible()
        expect(page.locator('[data-artifact-group="documents"]')).to_be_visible()
        if os.environ.get('WORKBENCH_ARTIFACT_TEST_SCREENSHOT'):
            page.screenshot(path=os.environ['WORKBENCH_ARTIFACT_TEST_SCREENSHOT'], full_page=True)
        expect(page.locator('[data-artifact-id="other"]')).to_be_visible()
        expect(page.locator('[data-artifact-id="hidden"]')).to_have_count(0)
        expect(page.locator('video')).to_have_count(0)
        page.locator('[data-artifact-id="video"] button').filter(has_text='播放').click()
        expect(page.locator('dialog[open] video')).to_be_visible()
        page.locator('dialog[open] video').evaluate("""video => {
            window.__lastPreviewVideo = video
            Object.defineProperty(video, 'paused', { configurable: true, value: false })
            video.pause = () => Object.defineProperty(video, 'paused', { configurable: true, value: true })
            video.load = () => {}
        }""")
        page.locator('dialog[open] button').filter(has_text='关闭').first.click()
        assert page.evaluate('window.__lastPreviewVideo.paused')
        assert page.evaluate("window.__lastPreviewVideo.getAttribute('src') === null")
        expect(page.locator('video')).to_have_count(0)
        page.locator('[data-artifact-id="cover"] button').filter(has_text='预览').click()
        expect(page.locator('dialog[open] img')).to_be_visible()
        page.locator('dialog[open] button').filter(has_text='关闭').first.click()
        page.locator('[data-artifact-id="notes"] button').filter(has_text='预览').click()
        expect(page.locator('dialog[open]')).to_contain_text('文本内容 notes')
        expect(page.locator('dialog[open] .markdown-preview h1')).to_have_text('文本内容 notes')
        expect(page.locator('dialog[open] .markdown-preview strong')).to_have_text('重点')
        assert page.evaluate('window.testXss === undefined')
        page.locator('dialog[open] button').filter(has_text='关闭').first.click()
        old_server[0] = True
        page.locator('[data-artifact-id="comment"] button').filter(has_text='预览').click()
        expect(page.locator('dialog[open] .markdown-preview h1')).to_have_text('本次采集评论')
        page.locator('dialog[open] button').filter(has_text='关闭').first.click()
        old_server[0] = False
        page.locator('[data-artifact-id="transcript-new"] button').filter(has_text='历史版本').click()
        page.locator('[data-artifact-id="transcript-old"] button').filter(has_text='预览').click()
        expect(page.locator('#editable-text-artifact')).to_have_value('transcript-old')
        page.locator('.markdown-editor-toolbar button').filter(has_text='预览').click()
        expect(page.locator('.markdown-editor-preview')).to_contain_text('文本内容 transcript-old')
        page.locator('#detail-tab-files').click()
        page.locator('.artifact-script-group summary').click()
        page.locator('[data-artifact-id="script-story"] button').filter(has_text='预览').click()
        expect(page.locator('#script-artifact-select')).to_have_value('script-story')
        page.locator('#detail-tab-files').click()
        expect(page.locator('[data-artifact-id="ocr"]')).to_contain_text('文件不可用')
        expect(page.locator('[data-artifact-id="ocr"] button').filter(has_text='打开文件')).to_be_disabled()
        page.locator('[data-artifact-id="notes"] button').filter(has_text='目录').click()
        assert opens and 'reveal=true' in opens[-1]
        page.locator('[data-artifact-id="notes"] button').filter(has_text='详情').click()
        expect(page.locator('dialog[open]')).to_contain_text('folder/notes.md')
        expect(page.locator('dialog[open] a')).to_have_attribute('download', 'notes.md')
        page.locator('dialog[open] button').filter(has_text='关闭').first.click()
        page.locator('[data-artifact-id="card"] button').filter(has_text='审核').click()
        expect(page.locator('#knowledge-card-select')).to_have_value('card')
        expect(page.locator('#detail-tab-knowledge')).to_have_attribute('aria-selected', 'true')
        expect(page.locator('.card-markdown-preview h1')).to_have_text('文本内容 card')
        expect(page.locator('.card-textarea')).to_have_count(0)
        page.get_by_role('button', name='编辑').click()
        expect(page.locator('.card-textarea')).to_have_value(re.compile('文本内容 card'))
        page.locator('.card-textarea').fill('# 保存后的知识卡\n\n已切换到编辑状态。')
        page.get_by_role('button', name='保存', exact=True).click()
        expect(page.locator('#knowledge-card-select')).to_have_value('card-revision')
        expect(page.locator('.card-markdown-preview h1')).to_have_text('保存后的知识卡')
        assert not errors, errors
        page.set_viewport_size({'width':390,'height':844})
        assert page.evaluate('document.documentElement.scrollWidth <= innerWidth')
        browser.close()
        print('PASS: artifact list, preview, exact version selection, unavailable file, folder action and mobile')
finally:
    server.terminate()
    server.wait(timeout=10)

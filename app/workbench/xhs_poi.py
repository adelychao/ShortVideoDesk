"""使用账号现有登录态查询小红书创作者地点。"""
from __future__ import annotations

import json
import os
from datetime import datetime, timezone
from pathlib import Path
import shutil
import subprocess
import tempfile

CATBUS_CLI = Path(__file__).resolve().parents[2] / 'third_party/xhs-poi-sdk/node_modules/catbus-cli/dist/cli/main.js'


class XhsPoiError(Exception):
    pass


def search_poi(account_file: Path, keyword: str) -> list[dict[str, str]]:
    keyword = keyword.strip()
    if not keyword or len(keyword) > 100:
        raise XhsPoiError('请输入有效的地点关键词。')
    node = shutil.which('node')
    if not node or not CATBUS_CLI.is_file():
        raise XhsPoiError('小红书地点搜索组件未安装，请安装项目的地点搜索依赖。')
    try:
        state = json.loads(account_file.read_text(encoding='utf-8'))
        cookies = []
        for item in state.get('cookies', []):
            domain = str(item.get('domain') or '').lstrip('.').lower()
            if domain != 'xiaohongshu.com' and not domain.endswith('.xiaohongshu.com'):
                continue
            if not isinstance(item.get('name'), str) or not isinstance(item.get('value'), str):
                continue
            expires = item.get('expires')
            cookies.append({'name': item['name'], 'value': item['value'], 'domain': item['domain'],
                            'path': item.get('path') or '/', 'expires': expires if isinstance(expires, (float, int)) and expires > 0 else None})
        now = datetime.now(timezone.utc).isoformat()
        credential = {'schema': 1, 'platform': 'xhs', 'endpoint': 'web', 'account': 'probe', 'user': None,
                      'method': 'cookie', 'created_at': now, 'updated_at': now,
                      'scopes': {'main': {'cookies': [], 'tokens': {}}, 'creator': {'cookies': cookies, 'tokens': {}}}, 'device': {}, 'extra': {}}
        with tempfile.TemporaryDirectory(prefix='workbench-xhs-poi-') as temporary:
            credential_file = Path(temporary) / 'auth/xhs/web/probe.json'
            credential_file.parent.mkdir(parents=True)
            credential_file.write_text(json.dumps(credential, ensure_ascii=False), encoding='utf-8')
            environment = os.environ.copy()
            environment['CATBUS_HOME'] = temporary
            result = subprocess.run([node, str(CATBUS_CLI), 'xhs', 'poi', 'search', keyword, '--account', 'probe'],
                                    env=environment, stdin=subprocess.DEVNULL, capture_output=True, text=True,
                                    encoding='utf-8', errors='replace', timeout=35, check=False,
                                    creationflags=getattr(subprocess, 'CREATE_NO_WINDOW', 0))
        payload = json.loads(result.stdout)
        if result.returncode != 0 or not isinstance(payload, dict) or payload.get('ok') is not True or not isinstance(payload.get('data'), list):
            raise XhsPoiError('小红书地点搜索失败，请检查账号登录状态后重试。')
        result_items = []
        for item in payload['data'][:50]:
            if not isinstance(item, dict):
                continue
            values = [item.get(key) for key in ('id', 'name', 'address')]
            if all(isinstance(v, str) and 0 < len(v.strip()) <= limit for v, limit in zip(values, (128, 100, 300))):
                result_items.append(dict(zip(('id', 'name', 'address'), (v.strip() for v in values))))
        return result_items
    except XhsPoiError:
        raise
    except (OSError, ValueError, TypeError, AttributeError, subprocess.SubprocessError):
        raise XhsPoiError('无法读取小红书地点，请检查账号和网络后重试。') from None

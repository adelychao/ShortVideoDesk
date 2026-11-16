import asyncio
import json

import pytest
from fastapi.exceptions import RequestValidationError
from fastapi import FastAPI
from fastapi.testclient import TestClient
from starlette.requests import Request

from app.workbench.api import _validation_error_handler
from app.workbench.config import AppConfig
from app.workbench.database import Database
from app.workbench import api
from app.workbench.runner import TaskRunner


def test_comment_settings_are_persistent_and_partial_updates_keep_them(tmp_path):
    config = AppConfig(Database(tmp_path / 'settings.sqlite3'))
    options = config.get_settings()['comment_analysis_options']
    options['batch_size'] = 300
    options['budget_cny'] = 2.0
    config.update_settings({'comment_analysis_options': options})
    config.update_settings({'vector_search_enabled': True})
    assert AppConfig(config.database).get_settings()['comment_analysis_options'] == options


def test_comment_settings_reject_invalid_output_limit(tmp_path):
    config = AppConfig(Database(tmp_path / 'settings.sqlite3'))
    options = config.get_settings()['comment_analysis_options']
    options['max_output_tokens'] = 1000000
    with pytest.raises(ValueError):
        config.update_settings({'comment_analysis_options': options})


def test_preview_validation_identifies_output_limit_without_echoing_input():
    request = Request({'type': 'http', 'method': 'GET', 'path': '/api/v1/tasks/t/comment-analysis-preview', 'headers': [], 'query_string': b''})
    error = RequestValidationError([{'loc': ('query', 'max_output_tokens'), 'type': 'less_than_equal', 'input': 1000000}])
    response = asyncio.run(_validation_error_handler(request, error))
    detail = json.loads(response.body)['detail']
    assert response.status_code == 422
    assert '32,768' in detail and '输出' in detail
    assert '1000000' not in detail


def test_config_http_roundtrip_and_legacy_save_preserves_options(tmp_path):
    database = Database(tmp_path / 'api.sqlite3')
    app = FastAPI()
    app.state.workbench_runner = TaskRunner(database, AppConfig(database))
    app.include_router(api._router)
    app.add_exception_handler(RequestValidationError, _validation_error_handler)
    client = TestClient(app, base_url='http://localhost')
    initial = client.get('/api/v1/config').json()['settings']
    initial['comment_analysis_options']['batch_size'] = 300
    response = client.put('/api/v1/config', json=initial)
    assert response.status_code == 200, response.text
    saved = response.json()['settings']
    assert saved['comment_analysis_options']['batch_size'] == 300
    invalid = {**saved, 'comment_analysis_options': {**saved['comment_analysis_options'], 'max_output_tokens': 1000000}}
    assert client.put('/api/v1/config', json=invalid).status_code == 422
    assert client.get('/api/v1/config').json()['settings']['comment_analysis_options']['max_output_tokens'] == 32768
    del saved['comment_analysis_options']
    response = client.put('/api/v1/config', json=saved)
    assert response.status_code == 200, response.text
    assert client.get('/api/v1/config').json()['settings']['comment_analysis_options']['batch_size'] == 300
    response = client.get('/api/v1/tasks/t/comment-analysis-preview?max_output_tokens=1000000')
    assert response.status_code == 422 and '32,768' in response.json()['detail']

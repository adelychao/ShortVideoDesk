import assert from 'node:assert/strict'
import { describe, it } from 'node:test'
import {
  createArticleEditorSnapshot,
  getArticlePublishEntryState,
} from '../src/articleFinalizePublish.ts'

describe('article publish entry state', () => {
  it('hides the publish entry for drafts', () => {
    assert.deepEqual(getArticlePublishEntryState('draft', false), {
      visible: false,
      enabled: false,
      helperText: '',
    })
  })

  it('enables the publish entry for a clean finalized article', () => {
    assert.deepEqual(getArticlePublishEntryState('finalized', false), {
      visible: true,
      enabled: true,
      helperText: '',
    })
  })

  it('keeps the publish entry visible but disabled for unsaved finalized edits', () => {
    assert.deepEqual(getArticlePublishEntryState('finalized', true), {
      visible: true,
      enabled: false,
      helperText: '请先保存修改并重新设为定稿，再发布。',
    })
  })
})

describe('article editor snapshot', () => {
  it('ignores trailing newlines added by rich-text editor serialization', () => {
    const original = { title: '标题', body_markdown: '正文内容' }
    const editorValue = { ...original, body_markdown: '正文内容\n' }

    assert.equal(createArticleEditorSnapshot(original), createArticleEditorSnapshot(editorValue))
  })

  it('still detects substantive edits at the end of the body', () => {
    const original = { title: '标题', body_markdown: '正文内容' }
    const edited = { ...original, body_markdown: '正文内容补充' }

    assert.notEqual(createArticleEditorSnapshot(original), createArticleEditorSnapshot(edited))
  })
})

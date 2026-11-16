import assert from 'node:assert/strict'
import { describe, it } from 'node:test'
import {
  closeArticleSourceTabAfterNavigation,
  getArticleSourceLeaveDecision,
} from '../src/articleSourceNavigation.ts'

describe('article source leave decision', () => {
  it('allows navigation after generation succeeds even before submitting clears', () => {
    assert.equal(getArticleSourceLeaveDecision({
      hasContent: true,
      submitting: true,
      generationSucceeded: true,
    }), 'allow')
  })

  it('blocks leaving while generation is still pending', () => {
    assert.equal(getArticleSourceLeaveDecision({
      hasContent: true,
      submitting: true,
      generationSucceeded: false,
    }), 'block')
  })

  it('asks before discarding source text when generation has not succeeded', () => {
    assert.equal(getArticleSourceLeaveDecision({
      hasContent: true,
      submitting: false,
      generationSucceeded: false,
    }), 'confirm')
  })

  it('allows leaving when the source is empty', () => {
    assert.equal(getArticleSourceLeaveDecision({
      hasContent: false,
      submitting: false,
      generationSucceeded: false,
    }), 'allow')
  })
})

describe('generated article navigation', () => {
  it('closes the source tab only after navigation and route state have settled', async () => {
    const events = []
    const navigated = await closeArticleSourceTabAfterNavigation(
      Promise.resolve(undefined),
      (result) => Boolean(result),
      async () => { events.push('route-settled') },
      async () => { events.push('source-tab-closed') },
    )

    assert.equal(navigated, true)
    assert.deepEqual(events, ['route-settled', 'source-tab-closed'])
  })

  it('keeps the source tab open when navigation fails', async () => {
    const events = []
    const navigated = await closeArticleSourceTabAfterNavigation(
      Promise.resolve({ type: 'aborted' }),
      (result) => Boolean(result),
      async () => { events.push('route-settled') },
      async () => { events.push('source-tab-closed') },
    )

    assert.equal(navigated, false)
    assert.deepEqual(events, [])
  })
})

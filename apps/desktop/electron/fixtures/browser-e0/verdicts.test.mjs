import assert from 'node:assert/strict'
import test from 'node:test'

import { aggregateVerdict } from './verdicts.mjs'

test('an unexecuted matrix is NOT_RUN rather than a vacuous PASS', () => {
  assert.equal(aggregateVerdict([{ id: 'S-2/P6', outcome: 'fail' }], 'S-2N/'), 'NOT_RUN')
})

test('executed matrices aggregate pass and failure normally', () => {
  assert.equal(aggregateVerdict([{ id: 'S-2N/direct-file', outcome: 'pass' }], 'S-2N/'), 'PASS')
  assert.equal(aggregateVerdict([{ id: 'S-2N/direct-file', outcome: 'fail' }], 'S-2N/'), 'FAIL')
})
/**
 * Checks for the conflict resolver's text handling.
 *
 *   node scripts/verify-conflicts.mjs
 *
 * The resolver used to decode file content with atob/btoa, which treats bytes as
 * characters: Urdu, accented letters and emoji came out garbled, and saving an edit wrote
 * them back corrupted. These pin the UTF-8 round trip, and the marker parsing the
 * "Mark as resolved" button depends on, against the exact format the server produces.
 */

import {
  applyChoice, applyChoiceToAll, b64Size, b64ToText, bytesToB64, hasConflictMarkers,
  parseConflictBlocks, textToB64,
} from '../src/lib/conflictText.js'

let failures = 0
const check = (name, ok, detail = '') => {
  if (ok) console.log(`  PASS  ${name}`)
  else {
    failures += 1
    console.log(`  FAIL  ${name}${detail ? `  ${detail}` : ''}`)
  }
}

// What the server sends: base64 of the UTF-8 bytes.
const serverB64 = (text) => Buffer.from(text, 'utf8').toString('base64')
const serverText = (b64) => Buffer.from(b64, 'base64').toString('utf8')

console.log('encoding')
for (const [label, sample] of [
  ['ascii', 'plain text\n'],
  ['urdu', 'سلام دنیا — خوش آمدید\n'],
  ['accents', 'café naïve résumé\n'],
  ['emoji', 'shipped ✅ 🚀\n'],
  ['crlf', 'line one\r\nline two\r\n'],
]) {
  const decoded = b64ToText(serverB64(sample))
  check(`${label}: decodes to the original text`, decoded === sample, JSON.stringify(decoded))
  check(`${label}: re-encodes to the same bytes`, serverText(textToB64(decoded)) === sample)
}

const bom = '﻿with a byte-order mark\n'
check('a byte-order mark survives the round trip', b64ToText(serverB64(bom)) === bom)

const notUtf8 = Buffer.from([0xff, 0xfe, 0x00, 0x81]).toString('base64')
check('invalid UTF-8 is reported as binary (null), not garbled', b64ToText(notUtf8) === null)

const big = 'x'.repeat(200_000) + '✓\n'
check('a 200 KB file round-trips without overflowing the stack',
  serverText(textToB64(b64ToText(serverB64(big)))) === big)

const bytes = new Uint8Array([0, 1, 2, 250, 251, 255])
check('raw bytes round-trip for binary files',
  Buffer.from(bytesToB64(bytes), 'base64').equals(Buffer.from(bytes)))
check('b64Size reports the decoded length', b64Size(serverB64('hello')) === 5)

console.log('\nconflict blocks')

// Byte-for-byte what app/services/merge.py renders for one conflict.
const merged =
  'one\n' +
  '<<<<<<< main\nmain line\n' +
  '||||||| base\nbase line\n' +
  '=======\nfeature line\n' +
  '>>>>>>> feature\n' +
  'three\n'

const [block] = parseConflictBlocks(merged)
check('finds the block the server renders', !!block)
check('reads each side', block && block.ours === 'main line\n' && block.base === 'base line\n'
  && block.theirs === 'feature line\n')
check('reads the branch labels', block && block.oursLabel === 'main' && block.theirsLabel === 'feature')
check('reports the line the block starts on', block && block.line === 2)

check('accept ours', applyChoice(merged, block, 'ours') === 'one\nmain line\nthree\n')
check('accept theirs', applyChoice(merged, block, 'theirs') === 'one\nfeature line\nthree\n')
check('accept both keeps target then source',
  applyChoice(merged, block, 'both') === 'one\nmain line\nfeature line\nthree\n')

const twoWay = 'a\n<<<<<<< HEAD\nx\n=======\ny\n>>>>>>> topic\nb\n'
check('a block without a base section is still a block',
  parseConflictBlocks(twoWay).length === 1 && parseConflictBlocks(twoWay)[0].base === '')

const crlf = merged.replace(/\n/g, '\r\n')
const crlfBlocks = parseConflictBlocks(crlf)
check('CRLF files parse', crlfBlocks.length === 1 && crlfBlocks[0].theirs === 'feature line\r\n')
check('resolving a CRLF block keeps CRLF',
  applyChoice(crlf, crlfBlocks[0], 'theirs') === 'one\r\nfeature line\r\nthree\r\n')

check('a Markdown underline is not a conflict', !hasConflictMarkers('Title\n=======\nbody\n'))
check('an unclosed block is not a conflict', !hasConflictMarkers('<<<<<<< main\nx\n=======\ny\n'))

const several = merged + merged.replace('one\n', '')
check('several blocks are found in order', parseConflictBlocks(several).length === 2)
const settled = applyChoiceToAll(several, 'theirs')
check('accepting a side for every block leaves no markers', !hasConflictMarkers(settled),
  JSON.stringify(settled))

console.log()
if (failures) {
  console.log(`${failures} check(s) failed`)
  process.exit(1)
}
console.log('all checks passed')

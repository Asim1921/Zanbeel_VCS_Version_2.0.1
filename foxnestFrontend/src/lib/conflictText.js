/**
 * Text handling for the merge conflict resolver.
 *
 * Kept free of React so it can be checked with plain node (scripts/verify-conflicts.mjs).
 *
 * The server sends file content as base64 of the raw bytes. `atob` turns that into a
 * "binary string" -- one character per byte -- which is not text: every character outside
 * ASCII showed up as two or three garbage characters, and `btoa` on edited text either
 * wrote invalid UTF-8 or threw and fell back to double-encoding the whole file. These
 * decode and encode real UTF-8, and leave a byte-order mark where it was.
 */

export function b64ToBytes(b64) {
  if (!b64) return new Uint8Array(0)
  const binary = atob(b64)
  const bytes = new Uint8Array(binary.length)
  for (let i = 0; i < binary.length; i += 1) bytes[i] = binary.charCodeAt(i)
  return bytes
}

export function bytesToB64(bytes) {
  let binary = ''
  // Chunked: spreading a large file into one String.fromCharCode call overflows the stack.
  const chunk = 0x8000
  for (let i = 0; i < bytes.length; i += chunk) {
    binary += String.fromCharCode.apply(null, bytes.subarray(i, i + chunk))
  }
  return btoa(binary)
}

/** UTF-8 text from base64, or null when the bytes are not valid UTF-8 (binary content). */
export function b64ToText(b64) {
  if (b64 == null) return null
  try {
    return new TextDecoder('utf-8', { fatal: true, ignoreBOM: true }).decode(b64ToBytes(b64))
  } catch {
    return null
  }
}

export function textToB64(text) {
  return bytesToB64(new TextEncoder().encode(text ?? ''))
}

/** Decoded size in bytes of a base64 payload, without decoding it. */
export function b64Size(b64) {
  if (!b64) return 0
  const padding = b64.endsWith('==') ? 2 : b64.endsWith('=') ? 1 : 0
  return Math.floor((b64.length * 3) / 4) - padding
}

const OPEN = /^<{7}(?: .*)?$/
const BASE = /^\|{7}(?: .*)?$/
const SEP = /^={7}$/
const CLOSE = /^>{7}(?: .*)?$/

function splitWithOffsets(text) {
  const lines = []
  let pos = 0
  while (pos < text.length) {
    const nl = text.indexOf('\n', pos)
    const end = nl === -1 ? text.length : nl + 1
    let content = text.slice(pos, nl === -1 ? text.length : nl)
    if (content.endsWith('\r')) content = content.slice(0, -1)
    lines.push({ start: pos, end, content })
    pos = end
  }
  return lines
}

/**
 * Complete conflict blocks in `text`, in order.
 *
 * A block needs its opening, separator and closing lines; a lone "=======" (a Markdown
 * underline) is not a conflict. That is the same rule the server applies when it refuses
 * a resolution with markers left in, so "Mark as resolved" and the server agree.
 *
 * Each block carries character offsets of the whole block and the text of each side.
 */
export function parseConflictBlocks(text) {
  const blocks = []
  if (!text) return blocks
  const lines = splitWithOffsets(text)

  let i = 0
  while (i < lines.length) {
    if (!OPEN.test(lines[i].content)) {
      i += 1
      continue
    }
    const open = i
    let base = -1
    let sep = -1
    let close = -1
    for (let j = open + 1; j < lines.length; j += 1) {
      const content = lines[j].content
      if (OPEN.test(content)) break // a new block opens before this one closed
      if (sep === -1 && base === -1 && BASE.test(content)) base = j
      else if (sep === -1 && SEP.test(content)) sep = j
      else if (sep !== -1 && CLOSE.test(content)) {
        close = j
        break
      }
    }
    if (sep === -1 || close === -1) {
      i += 1
      continue
    }

    const oursEnd = base !== -1 ? base : sep
    blocks.push({
      index: blocks.length,
      start: lines[open].start,
      end: lines[close].end,
      line: open + 1,
      oursLabel: lines[open].content.slice(8),
      theirsLabel: lines[close].content.slice(8),
      ours: text.slice(lines[open].end, lines[oursEnd].start),
      base: base !== -1 ? text.slice(lines[base].end, lines[sep].start) : '',
      theirs: text.slice(lines[sep].end, lines[close].start),
    })
    i = close + 1
  }
  return blocks
}

export function hasConflictMarkers(text) {
  return parseConflictBlocks(text).length > 0
}

/**
 * Replace one block with a side: 'ours' (the target branch), 'theirs' (the source
 * branch), 'both' (target then source, as GitHub's "Accept both changes") or 'base'.
 */
export function applyChoice(text, block, choice) {
  let replacement
  if (choice === 'ours') replacement = block.ours
  else if (choice === 'theirs') replacement = block.theirs
  else if (choice === 'base') replacement = block.base
  else replacement = block.ours + block.theirs
  return text.slice(0, block.start) + replacement + text.slice(block.end)
}

/** Apply one choice to every block, last first so earlier offsets stay valid. */
export function applyChoiceToAll(text, choice) {
  const blocks = parseConflictBlocks(text)
  let result = text
  for (let i = blocks.length - 1; i >= 0; i -= 1) {
    result = applyChoice(result, blocks[i], choice)
  }
  return result
}

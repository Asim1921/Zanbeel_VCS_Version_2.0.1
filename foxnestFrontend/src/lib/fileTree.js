/**
 * Turning flat repository paths into something navigable.
 *
 * The server returns a flat map of `path -> metadata`; folders exist only as
 * prefixes. These helpers live apart from the component so the tree can be built
 * and tested without rendering anything.
 */

/** Group flat paths into `{ folders: {name: node}, files: [path] }`. */
export function buildFileTree(paths) {
  const root = { folders: {}, files: [] }
  for (const filePath of paths) {
    const parts = String(filePath).split('/').filter(Boolean)
    let node = root
    parts.forEach((part, index) => {
      if (index === parts.length - 1) {
        node.files.push(filePath)
      } else {
        if (!node.folders[part]) node.folders[part] = { folders: {}, files: [] }
        node = node.folders[part]
      }
    })
  }
  return root
}

/** Every folder path on the way to `filePath`, so a deep file can be revealed. */
export function ancestorsOf(filePath) {
  const parts = String(filePath || '').split('/').filter(Boolean)
  parts.pop()
  const out = []
  let acc = ''
  for (const part of parts) {
    acc = acc ? `${acc}/${part}` : part
    out.push(acc)
  }
  return out
}

/** Total files at or below a node. */
export function countFiles(node) {
  let total = node.files.length
  for (const key of Object.keys(node.folders)) total += countFiles(node.folders[key])
  return total
}

/**
 * One spelling for comparisons.
 *
 * Paths are stored with whichever separator the pushing client used, so the same
 * file can arrive as `a/b.py` from one endpoint and `a\b.py` from another. Compare
 * the canonical form, never the raw string.
 */
export function normalizePath(path) {
  return String(path || '').split(String.fromCharCode(92)).join('/')
}

import React, { useMemo, useState } from 'react'
import { FiChevronDown, FiChevronRight, FiFolder, FiFile } from 'react-icons/fi'
import { cn } from '../lib/utils'
import { buildFileTree, countFiles } from '../lib/fileTree'

/**
 * A nested, navigable file tree built from flat paths.
 *
 * The server hands back a flat map of `path -> metadata`; folders only exist as
 * prefixes of those paths. This turns them back into something you can click
 * through, which is what the repository views needed and did not have.
 */

function TreeNode({ node, path, level, expanded, onToggle, selected, onSelect }) {
  const folderNames = Object.keys(node.folders).sort((a, b) => a.localeCompare(b))
  const files = [...node.files].sort((a, b) =>
    a.split('/').pop().localeCompare(b.split('/').pop())
  )

  return (
    <>
      {/* Folders first, then files — the order every file browser uses. */}
      {folderNames.map((name) => {
        const folderPath = path ? `${path}/${name}` : name
        const isOpen = expanded.has(folderPath)
        return (
          <div key={folderPath}>
            <button
              type="button"
              onClick={() => onToggle(folderPath)}
              aria-expanded={isOpen}
              className="group flex w-full items-center rounded px-2 py-1.5 text-left transition hover:bg-white/[0.05]"
              style={{ paddingLeft: `${level * 12 + 8}px` }}
            >
              {isOpen ? (
                <FiChevronDown className="mr-1 h-3.5 w-3.5 shrink-0 text-muted" />
              ) : (
                <FiChevronRight className="mr-1 h-3.5 w-3.5 shrink-0 text-muted" />
              )}
              <FiFolder className="mr-2 h-3.5 w-3.5 shrink-0 text-info-fg" />
              <span className="truncate text-[13px] text-ink-soft group-hover:text-ink">{name}</span>
              <span className="ml-auto pl-2 font-mono text-[10px] text-muted">
                {countFiles(node.folders[name])}
              </span>
            </button>
            {isOpen && (
              <TreeNode
                node={node.folders[name]}
                path={folderPath}
                level={level + 1}
                expanded={expanded}
                onToggle={onToggle}
                selected={selected}
                onSelect={onSelect}
              />
            )}
          </div>
        )
      })}

      {files.map((filePath) => {
        const name = filePath.split('/').pop()
        const isSelected = selected === filePath
        return (
          <button
            key={filePath}
            type="button"
            onClick={() => onSelect(filePath)}
            title={filePath}
            className={cn(
              'group flex w-full items-center rounded px-2 py-1.5 text-left transition',
              isSelected
                ? 'bg-accent/15 text-ink'
                : 'text-ink-soft hover:bg-white/[0.05] hover:text-ink'
            )}
            style={{ paddingLeft: `${level * 12 + 26}px` }}
          >
            <FiFile className="mr-2 h-3.5 w-3.5 shrink-0 text-muted" />
            <span className="truncate text-[13px]">{name}</span>
          </button>
        )
      })}
    </>
  )
}

export default function FileTree({
  paths = [],
  selected,
  onSelect,
  expandedPaths,
  onExpandedChange,
  className,
}) {
  const tree = useMemo(() => buildFileTree(paths), [paths])

  // Uncontrolled by default so callers that do not care about expansion state
  // can drop this in, but controllable when a caller needs to reveal a path.
  const [internal, setInternal] = useState(() => new Set())
  const expanded = expandedPaths ?? internal
  const setExpanded = onExpandedChange ?? setInternal

  const toggle = (folderPath) => {
    const next = new Set(expanded)
    if (next.has(folderPath)) next.delete(folderPath)
    else next.add(folderPath)
    setExpanded(next)
  }

  if (!paths.length) {
    return <p className={cn('px-2 py-6 text-center text-sm text-muted', className)}>No files in this branch.</p>
  }

  return (
    <div className={cn('py-1', className)}>
      <TreeNode
        node={tree}
        path=""
        level={0}
        expanded={expanded}
        onToggle={toggle}
        selected={selected}
        onSelect={onSelect}
      />
    </div>
  )
}

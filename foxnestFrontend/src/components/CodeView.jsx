import React, { useMemo } from 'react'
import { cn } from '../lib/utils'
import { authorClass } from '../lib/blame'

/**
 * Three ways of reading the same file: plain, blamed, and diffed.
 *
 * They live together because they share the line-number gutter and the monospace
 * grid; splitting them produced three subtly different alignments.
 */

const GUTTER = 'select-none border-r border-border pr-2 text-right font-mono text-[10.5px] text-muted'

/** Plain source with line numbers. */
function PlainLines({ content }) {
  const lines = useMemo(() => String(content ?? '').split('\n'), [content])
  return (
    <table className="w-full border-collapse font-mono text-[12px] leading-[1.55]">
      <tbody>
        {lines.map((text, i) => (
          <tr key={i} className="hover:bg-white/[0.03]">
            <td className={cn(GUTTER, 'w-[1%] whitespace-nowrap align-top')}>{i + 1}</td>
            <td className="whitespace-pre-wrap break-words pl-3 align-top text-ink">{text || ' '}</td>
          </tr>
        ))}
      </tbody>
    </table>
  )
}

/** Source annotated with the commit and author that last touched each line. */
function BlameLines({ blame }) {
  const meta = blame?.commits || {}
  const lines = blame?.lines || []
  return (
    <table className="w-full border-collapse font-mono text-[12px] leading-[1.55]">
      <tbody>
        {lines.map((line, i) => {
          const info = meta[line.commit_id] || {}
          // Only label a line when the author changes, so a run of one person's
          // work reads as a single block instead of a repeated column of names.
          const previous = lines[i - 1]
          const isNewBlock = !previous || previous.commit_id !== line.commit_id
          return (
            <tr
              key={line.line ?? i}
              className={cn('hover:bg-white/[0.03]', isNewBlock && i > 0 && 'border-t border-border/60')}
              title={info.summary ? `${info.author || 'unknown'} · ${info.summary}` : undefined}
            >
              <td className="w-[1%] select-none whitespace-nowrap border-r border-border pr-2 align-top">
                {isNewBlock ? (
                  <span className={cn('font-mono text-[10.5px]', authorClass(info.author))}>
                    {(info.author || 'unknown').slice(0, 14)}
                  </span>
                ) : (
                  <span className="text-[10.5px] text-transparent">·</span>
                )}
              </td>
              <td className="w-[1%] select-none whitespace-nowrap border-r border-border px-2 align-top font-mono text-[10.5px] text-muted">
                {isNewBlock ? String(line.commit_id || '').slice(0, 8) : ''}
              </td>
              <td className={cn(GUTTER, 'w-[1%] whitespace-nowrap align-top')}>{line.line ?? i + 1}</td>
              <td className="whitespace-pre-wrap break-words pl-3 align-top text-ink">
                {line.content || ' '}
              </td>
            </tr>
          )
        })}
      </tbody>
    </table>
  )
}

const ROW_TONE = {
  added: 'bg-success-fg/10',
  removed: 'bg-danger-fg/10',
  changed: 'bg-warning-fg/10',
  same: '',
}

/**
 * Unified diff from the server's side-by-side rows.
 *
 * Rows arrive as {type, previous, current} with no line numbers, so both sides
 * are counted here. `changed` is expanded into a removal followed by an addition,
 * which is how a unified diff reads.
 */
function DiffLines({ rows }) {
  const rendered = useMemo(() => {
    const out = []
    let prevNo = 0
    let currNo = 0
    for (const row of rows || []) {
      if (row.type === 'same') {
        prevNo += 1; currNo += 1
        out.push({ tone: 'same', sign: ' ', prevNo, currNo, text: row.current })
      } else if (row.type === 'added') {
        currNo += 1
        out.push({ tone: 'added', sign: '+', prevNo: null, currNo, text: row.current })
      } else if (row.type === 'removed') {
        prevNo += 1
        out.push({ tone: 'removed', sign: '-', prevNo, currNo: null, text: row.previous })
      } else {
        if (row.previous) { prevNo += 1; out.push({ tone: 'removed', sign: '-', prevNo, currNo: null, text: row.previous }) }
        if (row.current) { currNo += 1; out.push({ tone: 'added', sign: '+', prevNo: null, currNo, text: row.current }) }
      }
    }
    return out
  }, [rows])

  return (
    <table className="w-full border-collapse font-mono text-[12px] leading-[1.55]">
      <tbody>
        {rendered.map((row, i) => (
          <tr key={i} className={ROW_TONE[row.tone]}>
            <td className={cn(GUTTER, 'w-[1%] whitespace-nowrap align-top')}>{row.prevNo ?? ''}</td>
            <td className={cn(GUTTER, 'w-[1%] whitespace-nowrap align-top')}>{row.currNo ?? ''}</td>
            <td className="w-[1%] select-none px-2 align-top text-muted">{row.sign}</td>
            <td className="whitespace-pre-wrap break-words pr-3 align-top text-ink">{row.text || ' '}</td>
          </tr>
        ))}
      </tbody>
    </table>
  )
}

export default function CodeView({ mode = 'code', content, blame, rows, className }) {
  return (
    <div className={cn('overflow-x-auto', className)}>
      {mode === 'blame' && <BlameLines blame={blame} />}
      {mode === 'diff' && <DiffLines rows={rows} />}
      {mode === 'code' && <PlainLines content={content} />}
    </div>
  )
}

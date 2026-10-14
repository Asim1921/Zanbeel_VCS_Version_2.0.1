import React from 'react'
import ReactMarkdown from 'react-markdown'
import remarkGfm from 'remark-gfm'
import { cn } from '../lib/utils'

/**
 * Markdown as the rest of the client renders text: release notes and generated
 * documentation.
 *
 * Both are untrusted. Release notes are written by anyone with write access, and the
 * documentation is written by a model from repository content. So raw HTML is never
 * rendered (react-markdown's default -- there is deliberately no rehype-raw), link
 * protocols other than http(s), mailto and relative paths are dropped, and a relative
 * link is handed to `onNavigate` rather than followed: the documents link to each
 * other by path, and those paths mean files in the docs tree, not pages of this site.
 */

const SAFE_PROTOCOL = /^(https?:|mailto:)/i

function resolveRelative(base, href) {
  const [path, hash] = href.split('#')
  if (!path) return { path: null, hash }
  const parts = (path.startsWith('/') ? [] : (base || '').split('/').slice(0, -1))
  for (const part of path.split('/')) {
    if (!part || part === '.') continue
    if (part === '..') parts.pop()
    else parts.push(part)
  }
  return { path: parts.join('/'), hash }
}

function urlTransform(url) {
  const value = String(url || '').trim()
  if (SAFE_PROTOCOL.test(value)) return value
  // Anything with a scheme that is not on the list (javascript:, data:, vbscript:...).
  if (/^[a-z][a-z0-9+.-]*:/i.test(value)) return ''
  return value
}

// react-markdown hands every component its syntax-tree node, which is not a DOM attribute.
function domProps(props) {
  const rest = { ...props }
  delete rest.node
  return rest
}

const styled = (Tag, className) => {
  function Styled(props) {
    return <Tag className={className} {...domProps(props)} />
  }
  return Styled
}

const BLOCKS = {
  h1: styled('h1', 'mb-3 mt-6 border-b border-border pb-2 font-display text-2xl font-medium tracking-tight text-ink first:mt-0'),
  h2: styled('h2', 'mb-2 mt-6 border-b border-border pb-1.5 font-display text-xl font-medium tracking-tight text-ink first:mt-0'),
  h3: styled('h3', 'mb-2 mt-5 text-base font-semibold text-ink first:mt-0'),
  h4: styled('h4', 'mb-2 mt-4 text-sm font-semibold text-ink first:mt-0'),
  p: styled('p', 'my-2.5 leading-relaxed text-ink-soft'),
  ul: styled('ul', 'my-2.5 list-disc space-y-1 pl-6 text-ink-soft'),
  ol: styled('ol', 'my-2.5 list-decimal space-y-1 pl-6 text-ink-soft'),
  li: styled('li', 'leading-relaxed'),
  blockquote: styled('blockquote', 'my-3 border-l-2 border-accent/50 pl-4 text-muted'),
  th: styled('th', 'border border-border bg-white/[0.04] px-3 py-1.5 font-semibold text-ink'),
  td: styled('td', 'border border-border px-3 py-1.5 align-top text-ink-soft'),
  pre: styled('pre', 'my-3 overflow-x-auto rounded-lg border border-border bg-cream-mid p-3 font-mono text-[12px] leading-relaxed text-ink-soft'),
}

export default function Markdown({ children, className, basePath = '', onNavigate }) {
  const components = {
    ...BLOCKS,
    hr: () => <hr className="my-5 border-border" />,
    table: (props) => (
      <div className="my-3 overflow-x-auto">
        <table className="w-full border-collapse text-left text-[13px]" {...domProps(props)} />
      </div>
    ),
    code: (props) => {
      const { className: lang, children: text, ...rest } = domProps(props)
      return lang
        ? <code className={cn('font-mono', lang)} {...rest}>{text}</code>
        : <code className="rounded bg-white/[0.07] px-1.5 py-0.5 font-mono text-[0.85em] text-ink" {...rest}>{text}</code>
    },
    img: ({ alt }) => (
      // Images in the documents name files the browser cannot fetch from here.
      <span className="rounded border border-dashed border-border px-1.5 py-0.5 text-[12px] text-muted">
        [image{alt ? `: ${alt}` : ''}]
      </span>
    ),
    a: ({ href = '', children: text }) => {
      if (!href) return <span className="text-ink">{text}</span>
      if (SAFE_PROTOCOL.test(href)) {
        return (
          <a href={href} target="_blank" rel="noopener noreferrer" className="text-accent hover:underline">
            {text}
          </a>
        )
      }
      if (href.startsWith('#')) {
        return <a href={href} className="text-accent hover:underline">{text}</a>
      }
      const target = resolveRelative(basePath, href)
      return (
        <a
          href={`#${target.path || ''}`}
          onClick={(e) => {
            e.preventDefault()
            if (target.path && onNavigate) onNavigate(target.path, target.hash)
          }}
          className="text-accent hover:underline"
        >
          {text}
        </a>
      )
    },
  }

  return (
    <div className={cn('text-sm', className)}>
      <ReactMarkdown remarkPlugins={[remarkGfm]} components={components} urlTransform={urlTransform}>
        {children || ''}
      </ReactMarkdown>
    </div>
  )
}
